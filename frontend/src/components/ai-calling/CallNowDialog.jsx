import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { Warning, PhoneOutgoing } from '@phosphor-icons/react';
import { aiApi, errMsg } from '../../pages/ai-calling/aiApi';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '../ui/dialog';
import { inputCls } from './constants';

const newKey = () => (window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}-key`);

// Enhancement 2 §27A — mandatory test-call scenario selector. Same whitelist
// as BatchPanel; "Pick a scenario…" is non-selectable so no purpose is chosen
// by default and the admin must explicitly pick one before the call is placed.
const PURPOSES = [
    { value: '', label: 'Pick a scenario…' },
    { value: 'booking_confirmation', label: 'Booking Confirmation' },
    { value: 'interview_confirmation', label: 'Pre-Interview Confirmation' },
    { value: 'missed_interview_followup', label: 'Missed-Interview Follow-up' },
];

const Row = ({ k, v, testid }) => (
    <div className="flex justify-between gap-4 text-sm py-1 border-b border-[#efede5] last:border-0">
        <span className="text-[#6b7280]">{k}</span><span className="font-semibold text-[#1a2332] text-right" data-testid={testid}>{v || '—'}</span>
    </div>
);

export const CallNowDialog = ({ candidate, onClose, onDone }) => {
    const [name, setName] = useState('');
    const [callPurpose, setCallPurpose] = useState('');
    const [busy, setBusy] = useState(false);
    const key = useMemo(() => (candidate ? newKey() : ''), [candidate]);
    const match = !!candidate && name.trim() === candidate.candidate_name.trim();
    const purposeLabel = PURPOSES.find(p => p.value === callPurpose)?.label || '';
    const canSubmit = match && !!callPurpose && !busy;

    const submit = async () => {
        if (!canSubmit) return;
        setBusy(true);
        try {
            const r = (await aiApi.post(`/test-candidates/${candidate.id}/call-now`, {
                idempotency_key: key,
                confirm_name: name.trim(),
                call_purpose: callPurpose,
            })).data;
            if (r.duplicate) toast.info('This call was already sent — no second call placed.');
            else if (r.state === 'accepted') toast.success(`Bolna accepted the ${purposeLabel} call. Follow it in Call History.`);
            else if (r.state === 'rejected') toast.error(`Bolna rejected the call: ${r.error}`);
            else toast.warning('Bolna response unclear — calling is locked while we check the status automatically.');
            onDone(r);
        } catch (e) { toast.error(errMsg(e, 'Call not placed')); setBusy(false); }
    };

    return (
        <Dialog open={!!candidate} onOpenChange={(o) => !o && !busy && onClose()}>
            <DialogContent data-testid="call-now-dialog" className="bg-[#fffdf7] max-w-lg">
                <DialogHeader>
                    <DialogTitle>Place one real test call</DialogTitle>
                    <DialogDescription>Only this recipient will be called, once. No automatic redial.</DialogDescription>
                </DialogHeader>
                {candidate && (
                    <div className="space-y-4">
                        <div className="flex gap-2 text-sm text-rose-800 bg-rose-50 border border-rose-200 rounded-xl px-3 py-2" data-testid="call-now-warning">
                            <Warning size={18} weight="fill" className="shrink-0 mt-0.5" />
                            This places a real, chargeable phone call through Bolna AI to the number below.
                        </div>
                        <div className="bg-white border border-[#e5e3d8] rounded-xl px-3 py-2">
                            <Row k="Candidate" v={candidate.candidate_name} testid="call-now-name" />
                            <Row k="Mobile" v={candidate.phone_masked} testid="call-now-phone" />
                            <Row k="Job role" v={candidate.job_role} />
                            <Row k="Interview (IST)" v={`${candidate.interview_date} ${candidate.interview_time}`} />
                            <Row k="Language" v={candidate.language} />
                            <Row k="Company" v={candidate.company_name} />
                            <Row k="Scenario" v={purposeLabel} testid="call-now-scenario" />
                        </div>
                        <label className="block text-xs text-[#6b7280]">
                            <span className="text-[#1a2332] font-semibold">Test-call scenario</span>
                            <span className="text-rose-700"> *</span>
                            <select
                                value={callPurpose}
                                onChange={e => setCallPurpose(e.target.value)}
                                data-testid="call-now-purpose-select"
                                className={`${inputCls} mt-1`}
                            >
                                {PURPOSES.map(p => (
                                    <option key={p.value} value={p.value} disabled={p.value === ''}>{p.label}</option>
                                ))}
                            </select>
                            <span className="block text-[11px] text-[#6b7280] mt-1" data-testid="call-now-purpose-hint">
                                Scenario is mandatory — it is passed to Bolna as <code className="font-mono">call_purpose</code> and recorded in Call History.
                            </span>
                        </label>
                        <label className="block text-xs text-[#6b7280]">Type <b className="text-[#1a2332]">{candidate.candidate_name}</b> to authorise this call
                            <input value={name} onChange={e => setName(e.target.value)} className={`${inputCls} mt-1`} data-testid="call-now-confirm-input" autoComplete="off" />
                        </label>
                        <div className="flex justify-end gap-2">
                            <button onClick={onClose} disabled={busy} data-testid="call-now-cancel-btn" className="px-4 py-2 rounded-lg border border-[#e5e3d8] bg-white text-sm font-semibold">Cancel</button>
                            <button
                                onClick={submit}
                                disabled={!canSubmit}
                                data-testid="call-now-confirm-btn"
                                title={!callPurpose ? 'Select a test-call scenario first' : (!match ? 'Type the candidate name exactly' : '')}
                                className="px-4 py-2 rounded-lg bg-rose-700 text-white text-sm font-semibold flex items-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-rose-800">
                                <PhoneOutgoing size={16} /> {busy ? 'Calling…' : 'Call now'}
                            </button>
                        </div>
                    </div>
                )}
            </DialogContent>
        </Dialog>
    );
};
