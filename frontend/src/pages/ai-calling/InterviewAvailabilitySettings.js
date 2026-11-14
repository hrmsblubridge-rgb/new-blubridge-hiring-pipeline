/**
 * Enhancement 1 — Admin editor for the Chennai in-person availability
 * question configuration. Draft → Preview → Publish, with version history
 * and audit trail. Lives under the existing admin shell so it can reuse the
 * authenticated cookie session.
 */
import { useEffect, useState } from 'react';
import axios from 'axios';
import { toast } from 'sonner';

const API = process.env.REACT_APP_BACKEND_URL;

const RESERVED_IDS = new Set([
    'cannot_travel', 'distance', 'virtual_preferred',
    'work_commit', 'timing', 'not_interested', 'other',
]);

export default function InterviewAvailabilitySettings() {
    const [current, setCurrent] = useState(null);
    const [draft, setDraft] = useState(null);
    const [versions, setVersions] = useState([]);
    const [audit, setAudit] = useState([]);
    const [tab, setTab] = useState('edit');
    const [busy, setBusy] = useState(false);

    const reload = async () => {
        setBusy(true);
        try {
            const [c, d, v, a] = await Promise.all([
                axios.get(`${API}/api/bb/availability-question/current`, { withCredentials: true }),
                axios.get(`${API}/api/bb/availability-question/draft`, { withCredentials: true }),
                axios.get(`${API}/api/bb/availability-question/versions`, { withCredentials: true }),
                axios.get(`${API}/api/bb/availability-question/audit`, { withCredentials: true }),
            ]);
            setCurrent(c.data);
            setDraft(d.data.draft || c.data);
            setVersions(v.data.versions || []);
            setAudit(a.data.audit || []);
        } catch (e) {
            toast.error(e?.response?.data?.detail || 'Failed to load config');
        } finally { setBusy(false); }
    };
    useEffect(() => { reload(); }, []);

    const saveDraft = async () => {
        setBusy(true);
        try {
            await axios.post(`${API}/api/bb/availability-question/draft`,
                { config: draft }, { withCredentials: true });
            toast.success('Draft saved');
            reload();
        } catch (e) {
            toast.error(e?.response?.data?.detail || 'Save failed');
        } finally { setBusy(false); }
    };

    const publish = async () => {
        if (!window.confirm('Publish this version to all candidates? Existing responses stay intact.')) return;
        setBusy(true);
        try {
            const r = await axios.post(`${API}/api/bb/availability-question/publish`,
                { config: draft }, { withCredentials: true });
            toast.success(`Published version ${r.data.version}`);
            reload();
        } catch (e) {
            toast.error(e?.response?.data?.detail || 'Publish failed');
        } finally { setBusy(false); }
    };

    if (!draft) return <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6 text-zinc-500">Loading…</div>;

    const field = (k, label, long = false) => (
        <div key={k}>
            <label className="text-xs text-zinc-400 uppercase tracking-wider">{label}</label>
            {long
                ? <textarea value={draft[k] || ''}
                            onChange={e => setDraft({ ...draft, [k]: e.target.value })}
                            data-testid={`avail-field-${k}`}
                            className="w-full mt-1 bg-zinc-900 border border-zinc-800 p-2 text-sm min-h-[80px]"/>
                : <input type="text" value={draft[k] || ''}
                         onChange={e => setDraft({ ...draft, [k]: e.target.value })}
                         data-testid={`avail-field-${k}`}
                         className="w-full mt-1 bg-zinc-900 border border-zinc-800 p-2 text-sm"/>}
        </div>
    );

    const updateReason = (i, patch) => {
        const next = [...draft.reason_options];
        next[i] = { ...next[i], ...patch };
        setDraft({ ...draft, reason_options: next });
    };

    return (
        <div className="min-h-screen bg-zinc-950 text-zinc-100">
            <div className="p-6 max-w-4xl mx-auto space-y-6" data-testid="avail-settings">
                <header>
                    <h1 className="text-xl font-bold">Interview Scheduling — Chennai In-Person Availability</h1>
                    <p className="text-xs text-zinc-500 mt-1">
                        Published version {current?.version} · Editing draft based on version {current?.version}
                    </p>
                </header>

                <div className="flex gap-2 border-b border-zinc-800">
                    {['edit', 'preview', 'versions', 'audit'].map(t => (
                        <button key={t} onClick={() => setTab(t)} data-testid={`avail-tab-${t}`}
                                className={`px-3 py-1.5 text-sm border-b-2 ${tab === t ? 'border-cyan-400 text-white' : 'border-transparent text-zinc-500 hover:text-zinc-300'}`}>
                            {t.charAt(0).toUpperCase() + t.slice(1)}
                        </button>
                    ))}
                </div>

                {tab === 'edit' && (
                    <div className="space-y-4">
                        {field('main_question', 'Main question', true)}
                        <div className="grid grid-cols-2 gap-3">
                            {field('yes_label', 'YES label')}
                            {field('no_label', 'NO label')}
                        </div>
                        {field('reason_question', 'Reason question')}

                        <div>
                            <label className="text-xs text-zinc-400 uppercase tracking-wider">Reason options</label>
                            <p className="text-[11px] text-zinc-500 mt-0.5">Reserved IDs shown faded; labels editable, IDs frozen.</p>
                            <div className="mt-2 space-y-2" data-testid="avail-reason-options">
                                {draft.reason_options.map((r, i) => (
                                    <div key={r.id} className="flex items-center gap-2 bg-zinc-900 border border-zinc-800 p-2">
                                        <span className={`text-xs font-mono w-36 shrink-0 ${RESERVED_IDS.has(r.id) ? 'text-zinc-500' : 'text-zinc-300'}`}>
                                            {r.id}
                                        </span>
                                        <input value={r.label} onChange={e => updateReason(i, { label: e.target.value })}
                                               data-testid={`avail-reason-label-${r.id}`}
                                               className="flex-1 bg-transparent border border-zinc-800 p-1.5 text-sm"/>
                                        <label className="text-xs text-zinc-400 flex items-center gap-1">
                                            <input type="checkbox" checked={r.active !== false}
                                                   onChange={e => updateReason(i, { active: e.target.checked })}
                                                   data-testid={`avail-reason-active-${r.id}`}
                                                   className="accent-cyan-400"/>
                                            Active
                                        </label>
                                    </div>
                                ))}
                            </div>
                        </div>

                        {field('other_label', 'Other placeholder (50-char hint)')}
                        {field('explanation_text', 'In-person-only explanation', true)}

                        <div className="grid grid-cols-2 gap-3">
                            {field('withdraw_question', 'Withdrawal question')}
                            {field('withdraw_button_label', 'Withdrawal button label')}
                            {field('withdraw_yes_label', 'Withdrawal YES label')}
                            {field('withdraw_no_label', 'Withdrawal NO label')}
                        </div>
                        {field('withdraw_explanation', 'Withdrawal explanation', true)}
                        {field('withdraw_confirm_check', 'Withdrawal confirmation checkbox text', true)}
                        {field('withdraw_thankyou', 'Withdrawal thank-you message', true)}
                        {field('booked_no_notice', 'Booked + NO notice (shown read-only to candidate)', true)}

                        <div className="border-t border-zinc-800 pt-4 space-y-3">
                            {field('reactivation_question', 'Reactivation question', true)}
                            <div className="grid grid-cols-3 gap-3">
                                {field('reactivation_yes_label', 'YES label')}
                                {field('reactivation_no_label', 'NO label')}
                                {field('reactivation_other_label', 'Other label')}
                            </div>
                            {field('reactivation_other_placeholder', 'Other placeholder')}
                            {field('reactivation_no_confirmation', 'NO confirmation message')}
                            {field('reactivation_other_confirmation', 'Other confirmation message')}
                        </div>

                        <div className="flex gap-3 justify-end pt-4 border-t border-zinc-800">
                            <button onClick={saveDraft} disabled={busy} data-testid="avail-save-draft"
                                    className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-sm">
                                Save Draft
                            </button>
                            <button onClick={publish} disabled={busy} data-testid="avail-publish"
                                    className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-white text-sm font-semibold">
                                Publish New Version
                            </button>
                        </div>
                    </div>
                )}

                {tab === 'preview' && (
                    <div className="border border-zinc-800 p-6 bg-zinc-900/50 space-y-4" data-testid="avail-preview">
                        <h3 className="font-bold">{draft.main_question}</h3>
                        <div className="text-sm text-zinc-300">○ {draft.yes_label}</div>
                        <div className="text-sm text-zinc-300">○ {draft.no_label}</div>
                        <div className="border-t border-zinc-800 pt-3 text-sm">
                            <div className="text-zinc-400 mb-2">{draft.reason_question}</div>
                            <ul className="text-zinc-300 list-disc list-inside">
                                {draft.reason_options.filter(r => r.active !== false).map(r => (
                                    <li key={r.id}>{r.label}</li>
                                ))}
                            </ul>
                        </div>
                        <div className="border-t border-zinc-800 pt-3 text-xs text-zinc-400 italic">
                            {draft.explanation_text}
                        </div>
                    </div>
                )}

                {tab === 'versions' && (
                    <div className="space-y-2" data-testid="avail-versions-list">
                        {versions.map(v => (
                            <div key={v.version} className="border border-zinc-800 p-3 text-sm">
                                <div className="flex items-center justify-between">
                                    <b>Version {v.version}</b>
                                    <span className="text-xs text-zinc-500">
                                        {String(v.published_at || v.snapshot_at || '').split('T')[0]} · by {v.published_by || '-'}
                                    </span>
                                </div>
                                <div className="text-xs text-zinc-400 mt-1">{v.main_question}</div>
                            </div>
                        ))}
                    </div>
                )}

                {tab === 'audit' && (
                    <div className="space-y-2" data-testid="avail-audit-list">
                        {audit.map((a, i) => (
                            <div key={i} className="border border-zinc-800 p-3 text-sm flex items-center justify-between">
                                <span>
                                    <b>{a.action}</b> · from v{a.from_version} → v{a.to_version}
                                </span>
                                <span className="text-xs text-zinc-500">{String(a.at || '').split('T')[0]} · {a.by}</span>
                            </div>
                        ))}
                        {audit.length === 0 && <div className="text-zinc-500 text-sm">No audit entries yet.</div>}
                    </div>
                )}
            </div>
        </div>
    );
}
