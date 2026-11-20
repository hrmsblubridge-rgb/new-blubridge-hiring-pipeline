/**
 * Manual OTP Verify (iter68 — date-aware, 2-step flow)
 * ----------------------------------------------------
 * Step 1: Search by email + phone → show applicant details.
 * Step 2: Show "Verify" button only if scheduled interview date is TODAY.
 *         - schedule_date < today  → button hidden, message "Your interview is over !"
 *         - schedule_date > today  → button hidden, message "Your interview is in future !"
 *         - schedule_date == today → "Verify" button visible.
 * On Verify success: pipeline_data.otp_verified = true (handled by backend).
 */
import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { toast } from 'sonner';
import {
    ArrowLeft, ShieldCheck, CheckCircle,
    Clock, ArrowLeft as ArrowLeftIcon,
} from '@phosphor-icons/react';
import { formatDateDDMMYYYY as fmtDate, formatTime12H as fmtTime } from '../utils/dateFormat';
import ApplicantSearchCards from '../components/ApplicantSearchCards';

const API = process.env.REACT_APP_BACKEND_URL;

// iter86 — Same slot grid as the public InterviewSchedule.js form.
const TIME_SLOTS = ['10:00 AM','10:30 AM','11:00 AM','11:30 AM','12:00 PM','12:30 PM','01:00 PM','01:30 PM','02:00 PM','02:30 PM','03:00 PM','03:30 PM','04:00 PM','04:30 PM','05:00 PM'];

function _slotMinutes(label) {
    const [tm, period] = label.split(' ');
    let [h, m] = tm.split(':').map(Number);
    if (period === 'PM' && h !== 12) h += 12;
    if (period === 'AM' && h === 12) h = 0;
    return h * 60 + m;
}
function _slotToHMS(label) {
    if (!label) return '';
    const m = _slotMinutes(label);
    const hh = Math.floor(m / 60), mm = m % 60;
    return `${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}:00`;
}
function _hmsToSlot(hms) {
    // Convert "13:30:00" → "01:30 PM" if matches a known slot; else the closest format
    if (!hms) return '';
    const [h, m] = hms.split(':').map(Number);
    if (isNaN(h) || isNaN(m)) return '';
    // iter86 — Match the display heuristic used elsewhere: hours < 6 are
    // legacy mis-stored PM slots → shift to PM for the dropdown selection.
    let hh = h;
    if (hh >= 1 && hh < 6) hh += 12;
    const period = hh >= 12 ? 'PM' : 'AM';
    const h12 = ((hh % 12) || 12);
    const label = `${String(h12).padStart(2,'0')}:${String(m).padStart(2,'0')} ${period}`;
    return TIME_SLOTS.includes(label) ? label : '';
}

// iter86 — Small row helper for the applicant details table. Keeps inline-edit
// inputs and read-only cells visually consistent without re-templating each row.
function Row({ k, v }) {
    return (
        <tr className="border-b border-[#ece9dc] last:border-b-0">
            <td className="px-5 py-2.5 text-[#6b7280] w-44 align-top">{k}</td>
            <td className="px-5 py-2.5 text-[#1a2332] font-medium">{v ?? '—'}</td>
        </tr>
    );
}

export default function ManualOtpVerify() {
    const navigate = useNavigate();
    const [query, setQuery] = useState('');
    const [loadingDetail, setLoadingDetail] = useState(false);
    const [verifying, setVerifying] = useState(false);
    const [applicant, setApplicant] = useState(null);  // lookup payload
    const [verified, setVerified] = useState(null);    // post-verify applicant payload
    // iter163 — Undo + walk-in modal state.
    const [undoModal, setUndoModal] = useState({ open: false, reason: '', text: '' });
    const [walkInModal, setWalkInModal] = useState({ open: false, presenceConfirmed: false, chennaiConfirmed: false });
    // iter163b — Admin direct revoke state + the caller's effective perms.
    const [myPerms, setMyPerms] = useState({});
    const [adminRevokeModal, setAdminRevokeModal] = useState({ open: false, remarks: '' });

    useEffect(() => {
        axios.get(`${API}/api/bb/manual-otp/me`, { withCredentials: true })
            .then(r => setMyPerms(r.data?.permissions || {}))
            .catch(() => setMyPerms({}));
    }, []);

    const submitAdminRevoke = async () => {
        try {
            await axios.post(`${API}/api/bb/manual-otp/admin-revoke`, {
                registration_id: applicant.id || applicant._id,
                remarks: adminRevokeModal.remarks,
            }, { withCredentials: true });
            toast.success('Attendance revoked');
            setAdminRevokeModal({ open: false, remarks: '' });
            if (applicant.email || applicant.phone) selectApplicant({ email: applicant.email, phone: applicant.phone });
        } catch (err) { toast.error(err?.response?.data?.detail || 'Revoke failed'); }
    };

    const submitUndoRequest = async () => {
        try {
            await axios.post(`${API}/api/bb/manual-otp/reversal-requests`, {
                registration_id: applicant.id || applicant._id,
                reason_code: undoModal.reason,
                reason_text: undoModal.text,
            }, { withCredentials: true });
            toast.success('Correction request submitted for admin review');
            setUndoModal({ open: false, reason: '', text: '' });
        } catch (err) { toast.error(err?.response?.data?.detail || 'Submission failed'); }
    };

    const submitWalkIn = async () => {
        try {
            const r = await axios.post(`${API}/api/bb/manual-otp/walk-in`, {
                registration_id: applicant.id || applicant._id,
                physical_presence_confirmed: !!walkInModal.presenceConfirmed,
                chennai_availability_confirmed: !!walkInModal.chennaiConfirmed,
                accept_capacity_override: true,
            }, { withCredentials: true });
            toast.success(`Walk-in verified · slot ${r.data.assigned_interview_slot_ist}`);
            setWalkInModal({ open: false });
            // Re-fetch to show the new verified state.
            if (applicant.email || applicant.phone) selectApplicant({ email: applicant.email, phone: applicant.phone });
        } catch (err) { toast.error(err?.response?.data?.detail || 'Walk-in failed'); }
    };

    const selectApplicant = async (card) => {
        setLoadingDetail(true);
        setVerified(null);
        try {
            const params = {};
            if (card.email) params.email = card.email;
            if (card.phone) params.phone = card.phone;
            const r = await axios.get(`${API}/api/bb/manual/applicant/lookup`, {
                withCredentials: true, params,
            });
            setApplicant(r.data);
        } catch (e) {
            toast.error(e.response?.data?.detail || 'Failed to load applicant');
        } finally { setLoadingDetail(false); }
    };

    const backToResults = () => {
        setApplicant(null); setVerified(null);
        setRescheduling(false); setEdit({});
    };

    const handleVerify = async () => {
        setVerifying(true);
        try {
            const r = await axios.post(`${API}/api/bb/manual/otp/verify`,
                { email: applicant.email, phone: applicant.phone },
                { withCredentials: true }
            );
            setVerified(r.data.applicant);
            toast.success('OTP verified — applicant marked as Attended');
        } catch (e) {
            toast.error(e.response?.data?.detail || 'Verify failed');
        } finally { setVerifying(false); }
    };

    const handleCancel = () => {
        setQuery(''); setApplicant(null); setVerified(null);
        setRescheduling(false); setEdit({});
    };
    const [rescheduling, setRescheduling] = useState(false);
    const [edit, setEdit] = useState({});      // {phone,email,job_role,schedule_date,schedule_time}
    const [savingResch, setSavingResch] = useState(false);

    // iter104 — Synchronize reschedule date validation with the public
    // /schedule-interview flow: disallow Sundays + configured holidays.
    // Holidays are fetched once per page load from the admin endpoint; we
    // expand Recurring docs locally (same logic as the server's _expand_
    // holiday_dates) so MM-DD repeats across years.
    const [holidayDates, setHolidayDates] = useState([]);
    useEffect(() => {
        axios.get(`${API}/api/bb/holidays`, { withCredentials: true })
            .then(r => {
                const out = new Set();
                const thisYear = new Date().getFullYear();
                for (const h of (r.data?.holidays || [])) {
                    const d = (h.date || '').trim();
                    if (!d) continue;
                    if ((h.holiday_type || 'Recurring') === 'Recurring') {
                        const mmdd = d.slice(5);
                        for (let y = thisYear - 1; y <= thisYear + 2; y++) out.add(`${y}-${mmdd}`);
                    } else {
                        out.add(d);
                    }
                }
                setHolidayDates([...out]);
            })
            .catch(() => setHolidayDates([]));
    }, []);

    const isSunday = (d) => !!d && new Date(d).getDay() === 0;
    const isBlockedDate = (d) => isSunday(d) || holidayDates.includes(d);

    // YYYY-MM-DD comparison (today < scheduled)
    const _today = new Date().toISOString().slice(0, 10);
    const _schedISO = (applicant?.schedule_date || '').slice(0, 10);
    const canReschedule = !!_schedISO && _today < _schedISO;

    const startReschedule = () => {
        setRescheduling(true);
        setEdit({
            phone:         applicant.phone || '',
            email:         applicant.email || '',
            job_role:      applicant.job_role || '',
            schedule_date: (applicant.schedule_date || '').slice(0, 10),
            schedule_time: _hmsToSlot(applicant.schedule_time || ''),
        });
    };
    const cancelReschedule = () => { setRescheduling(false); setEdit({}); };

    const handleRescheduleVerify = async () => {
        // iter104 — Block Sundays + configured holidays at submit, in case
        // the user typed an ISO date into the input rather than picking it.
        if (isBlockedDate(edit.schedule_date)) {
            toast.error('Selected date is a Sunday or holiday. Please pick a working day.');
            return;
        }
        setSavingResch(true);
        try {
            // iter87 — Lock-at-5PM ONLY when selected date is today AND past 5 PM.
            const _now = new Date();
            const _isLockedToday = edit.schedule_date === _today && _now.getHours() >= 17;
            const slotLabel = _isLockedToday ? '05:00 PM' : edit.schedule_time;
            const r = await axios.post(`${API}/api/bb/manual/otp/reschedule-verify`,
                {
                    original_email: applicant.email,
                    original_phone: applicant.phone,
                    phone:         edit.phone,
                    email:         edit.email,
                    job_role:      edit.job_role,
                    schedule_date: edit.schedule_date,
                    schedule_time: _slotToHMS(slotLabel),
                },
                { withCredentials: true }
            );
            setVerified(r.data.applicant);
            setRescheduling(false);
            toast.success('Rescheduled and verified — applicant marked as Attended');
        } catch (e) {
            toast.error(e.response?.data?.detail || 'Reschedule & Verify failed');
        } finally { setSavingResch(false); }
    };

    return (
        <div className="min-h-screen" data-testid="manual-otp-verify-page">
            <header className="bg-[#faf9f1] border-b border-[#e5e3d8] px-6 lg:px-10 py-5">
                <div className="max-w-3xl mx-auto flex items-center gap-3 pl-12 lg:pl-0">
                    <button onClick={() => navigate('/home')} data-testid="back-btn" className="p-2 rounded-lg hover:bg-[#efede5]">
                        <ArrowLeft size={18} className="text-[#1a2332]" />
                    </button>
                    <div className="w-10 h-10 rounded-xl flex items-center justify-center bg-amber-100">
                        <ShieldCheck size={22} weight="duotone" className="text-amber-700" />
                    </div>
                    <div>
                        <h1 className="text-xl lg:text-2xl font-bold text-[#1a2332] tracking-tight">Manual OTP Verify</h1>
                        <p className="text-xs text-[#6b7280] mt-0.5">Mark a candidate as Attended (otp_verified = true)</p>
                    </div>
                </div>
            </header>

            <main className="max-w-3xl mx-auto px-6 lg:px-10 py-6 space-y-5">
                {/* Step 1 — Search (multi-card picker) */}
                {!applicant && !verified && (
                    <ApplicantSearchCards
                        value={query}
                        onChange={setQuery}
                        onSelect={selectApplicant}
                        onCancel={handleCancel}
                        testIdPrefix="manual-otp"
                        placeholder="Type name, email, or phone (min 2 chars)…"
                    />
                )}
                {loadingDetail && (
                    <div className="bg-[#fffdf7] border border-[#e5e3d8] rounded-2xl p-6 text-center text-sm text-[#6b7280]" data-testid="manual-otp-loading-detail">
                        Loading applicant…
                    </div>
                )}
                {(applicant || verified) && (
                    <div className="flex items-center justify-between gap-3">
                        <button onClick={backToResults} data-testid="manual-otp-back-to-results"
                            className="inline-flex items-center gap-2 text-sm font-semibold text-[#1d3a8a] hover:underline">
                            <ArrowLeftIcon size={14} weight="bold" /> Back to results
                        </button>
                        <span className="text-[11px] text-[#9b9787]">Showing details for <strong className="text-[#1a2332]">{(applicant || verified)?.name || (applicant || verified)?.email}</strong></span>
                    </div>
                )}

                {/* Step 2 — Applicant details + Verify / Reschedule & Verify */}
                {applicant && !verified && (
                    <div className="bg-[#fffdf7] border border-[#e5e3d8] rounded-2xl overflow-hidden" data-testid="manual-otp-applicant">
                        <div className="px-5 py-3 bg-[#faf9f1] border-b border-[#e5e3d8] text-sm font-semibold text-[#1a2332] flex items-center justify-between">
                            <span>Applicant Details</span>
                            {rescheduling && <span className="text-amber-700 text-xs">Editing — Phone / Email / Job Role / Date / Time</span>}
                        </div>
                        <table className="w-full text-sm">
                            <tbody>
                                <Row k="Name" v={applicant.name} />
                                <Row k="Phone" v={rescheduling
                                    ? <input value={edit.phone} onChange={(e) => setEdit(s => ({...s, phone: e.target.value}))} data-testid="resch-phone" className="w-full bg-white border border-[#e5e3d8] rounded px-2 py-1.5 text-sm" />
                                    : (applicant.phone || '—')} />
                                <Row k="Email" v={rescheduling
                                    ? <input value={edit.email} onChange={(e) => setEdit(s => ({...s, email: e.target.value}))} data-testid="resch-email" className="w-full bg-white border border-[#e5e3d8] rounded px-2 py-1.5 text-sm" />
                                    : (applicant.email || '—')} />
                                <Row k="Job Role" v={rescheduling
                                    ? <input value={edit.job_role} onChange={(e) => setEdit(s => ({...s, job_role: e.target.value}))} data-testid="resch-jobrole" className="w-full bg-white border border-[#e5e3d8] rounded px-2 py-1.5 text-sm" />
                                    : (applicant.job_role || '—')} />
                                <Row k="College Type" v={applicant.college_type} />
                                <Row k="Source (HR Team)" v={applicant.hr_team} />
                                <Row k="Schedule Date" v={rescheduling
                                    ? <div className="flex flex-col gap-0.5">
                                        <input type="date" value={edit.schedule_date} min={_today}
                                            onChange={(e) => {
                                                const v = e.target.value;
                                                // iter104 — Instant feedback when the picker (or
                                                // a typed value) lands on a Sunday/holiday. We
                                                // still let the value land in state so the user
                                                // sees what they picked, but show a clear inline
                                                // warning + block save at submit time.
                                                if (isBlockedDate(v)) {
                                                    toast.warning('That date is a Sunday or configured holiday — pick a working day.');
                                                }
                                                setEdit(s => ({...s, schedule_date: v}));
                                            }}
                                            data-testid="resch-date"
                                            className={`bg-white border rounded px-2 py-1.5 text-sm ${isBlockedDate(edit.schedule_date) ? 'border-rose-500 text-rose-700' : 'border-[#e5e3d8]'}`} />
                                        {isBlockedDate(edit.schedule_date) && (
                                            <span className="text-[11px] text-rose-600" data-testid="resch-date-blocked">
                                                {isSunday(edit.schedule_date) ? 'Sundays are not allowed' : 'This is a holiday'}
                                            </span>
                                        )}
                                      </div>
                                    : (fmtDate(applicant.schedule_date) || '—')} />
                                <Row k="Schedule Time" v={rescheduling ? (() => {
                                    // iter87 — Use the same TIME_SLOTS dropdown as the public
                                    // Interview Schedule form. Lock-at-5PM ONLY applies when
                                    // the selected date IS today AND current local hour ≥ 17.
                                    // Future dates always show a fully-editable dropdown.
                                    const _now = new Date();
                                    const _isToday = edit.schedule_date === _today;
                                    const _isAfter5PM = _isToday && _now.getHours() >= 17;
                                    const _minMins = _isToday ? (_now.getHours() * 60 + _now.getMinutes()) : -1;
                                    if (_isAfter5PM) {
                                        return (
                                            <div className="flex items-center gap-2 text-sm" data-testid="resch-time-locked">
                                                <span className="font-semibold text-[#1a2332]">05:00 PM</span>
                                                <span className="text-[11px] text-[#9b9787]">(locked — current time is past 5 PM)</span>
                                            </div>
                                        );
                                    }
                                    return (
                                        <select value={edit.schedule_time} onChange={(e) => setEdit(s => ({...s, schedule_time: e.target.value}))} data-testid="resch-time"
                                            className="bg-white border border-[#e5e3d8] rounded px-2 py-1.5 text-sm">
                                            <option value="">Select Time</option>
                                            {TIME_SLOTS.map(t => {
                                                const disabled = _minMins >= 0 && _slotMinutes(t) <= _minMins;
                                                return <option key={t} value={t} disabled={disabled}>{t}{disabled ? '  (past)' : ''}</option>;
                                            })}
                                        </select>
                                    );
                                })() : (fmtTime(applicant.schedule_time) || '—')} />
                                <Row k="OTP" v={applicant.otp} />
                                <Row k="Currently Verified?" v={applicant.otp_verified ? 'Yes' : 'No'} />
                            </tbody>
                        </table>

                        {/* iter82 — Always-enabled Verify + optional Reschedule & Verify */}
                        <div className="px-5 py-4 border-t border-[#ece9dc] bg-[#faf9f1] flex flex-wrap gap-3">
                            {applicant.otp_verified ? (
                                <div className="w-full">
                                    <div className="flex items-center gap-2 text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3 text-sm font-semibold"
                                        data-testid="manual-otp-status-already-verified">
                                        <CheckCircle size={18} weight="fill" />
                                        Applicant has already verified their OTP !
                                    </div>
                                    {/* iter163 — Request Undo Verification (Feature A). Opens a
                                        mandatory-reason modal; backend enforces the per-feature
                                        permission flag `can_request_otp_reversal`. */}
                                    <button onClick={() => setUndoModal({ open: true, reason: '', text: '' })}
                                        data-testid="manual-otp-undo-btn"
                                        className="mt-3 px-4 py-2 rounded-lg border border-amber-300 bg-amber-50 hover:bg-amber-100 text-amber-800 font-semibold text-sm flex items-center gap-2">
                                        <ArrowLeftIcon size={14} weight="bold" /> Request Undo Verification
                                    </button>
                                    {/* iter163b — Admin direct revoke. Only shown when the caller
                                        has can_approve_otp_reversal; backend 403s anyone else. */}
                                    {myPerms.can_approve_otp_reversal && (
                                        <button onClick={() => setAdminRevokeModal({ open: true, remarks: '' })}
                                            data-testid="manual-otp-admin-revoke-btn"
                                            className="mt-3 ml-2 px-4 py-2 rounded-lg border border-red-300 bg-red-50 hover:bg-red-100 text-red-800 font-semibold text-sm flex items-center gap-2">
                                            <ArrowLeftIcon size={14} weight="bold" /> Admin: Revoke Attendance
                                        </button>
                                    )}
                                </div>
                            ) : rescheduling ? (
                                <>
                                    <button onClick={handleRescheduleVerify} disabled={savingResch} data-testid="manual-otp-resched-verify-save"
                                        className="px-5 py-2.5 rounded-lg bg-amber-600 hover:bg-amber-700 text-white font-semibold text-sm flex items-center gap-2 disabled:opacity-60">
                                        <ShieldCheck size={16} weight="bold" /> {savingResch ? 'Saving…' : 'Save & Verify'}
                                    </button>
                                    <button onClick={cancelReschedule} disabled={savingResch} data-testid="manual-otp-resched-cancel"
                                        className="px-5 py-2.5 rounded-lg border border-[#e5e3d8] bg-[#fffdf7] text-[#1a2332] font-semibold text-sm hover:bg-[#efede5]">
                                        Discard changes
                                    </button>
                                </>
                            ) : (
                                <>
                                    <button onClick={handleVerify} disabled={verifying} data-testid="manual-otp-verify-btn"
                                        className="px-5 py-2.5 rounded-lg bg-amber-600 hover:bg-amber-700 text-white font-semibold text-sm flex items-center gap-2 disabled:opacity-60">
                                        <ShieldCheck size={16} weight="bold" /> {verifying ? 'Verifying…' : 'Verify'}
                                    </button>
                                    {canReschedule && (
                                        <button onClick={startReschedule} data-testid="manual-otp-reschedule-verify-btn"
                                            className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white font-semibold text-sm flex items-center gap-2">
                                            <Clock size={16} weight="bold" /> Reschedule & Verify
                                        </button>
                                    )}
                                    {/* iter163 — Schedule & Verify Walk-in (Feature B). Backend
                                        enforces `can_schedule_walkin`, candidate eligibility
                                        (shortlisted, not withdrawn, chennai YES), and the
                                        IST slot calculation. */}
                                    <button onClick={() => setWalkInModal({ open: true, chennaiConfirmed: applicant.chennai_availability_latest === false, capacityOverride: false })}
                                        data-testid="manual-otp-walkin-btn"
                                        className="px-5 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-semibold text-sm flex items-center gap-2">
                                        <ShieldCheck size={16} weight="bold" /> Schedule &amp; Verify Walk-in
                                    </button>
                                </>
                            )}
                        </div>
                    </div>
                )}
                {/* iter163 — Undo modal */}
                {undoModal.open && (
                    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" data-testid="manual-otp-undo-modal">
                        <div className="bg-white rounded-xl max-w-md w-full p-6 space-y-4">
                            <h3 className="font-semibold text-[#1a2332]">Request Undo Verification</h3>
                            <p className="text-xs text-[#6b7280]">Attendance stays VERIFIED until a top-level admin reviews. Reason is required.</p>
                            <select value={undoModal.reason} onChange={e => setUndoModal({ ...undoModal, reason: e.target.value })}
                                data-testid="manual-otp-undo-reason"
                                className="w-full border border-[#e5e3d8] rounded-lg px-3 py-2 text-sm">
                                <option value="">Select reason…</option>
                                <option value="wrong_candidate">Wrong candidate verified</option>
                                <option value="accidental">Attendance verified accidentally</option>
                                <option value="not_present">Candidate not physically present</option>
                                <option value="incorrect_record">Incorrect verification record</option>
                                <option value="other">Other — specify below</option>
                            </select>
                            {undoModal.reason === 'other' && (
                                <textarea value={undoModal.text} onChange={e => setUndoModal({ ...undoModal, text: e.target.value })}
                                    data-testid="manual-otp-undo-text"
                                    placeholder="Explain the correction…" rows={3}
                                    className="w-full border border-[#e5e3d8] rounded-lg px-3 py-2 text-sm" />
                            )}
                            <div className="flex justify-end gap-2 pt-2">
                                <button onClick={() => setUndoModal({ open: false, reason: '', text: '' })}
                                    data-testid="manual-otp-undo-cancel"
                                    className="px-3 py-2 rounded-lg border border-[#e5e3d8] text-sm">Cancel</button>
                                <button onClick={submitUndoRequest} disabled={!undoModal.reason || (undoModal.reason === 'other' && !undoModal.text.trim())}
                                    data-testid="manual-otp-undo-submit"
                                    className="px-3 py-2 rounded-lg bg-amber-600 hover:bg-amber-700 text-white text-sm disabled:opacity-50">Submit Request</button>
                            </div>
                        </div>
                    </div>
                )}
                {/* iter163b — Admin direct-revoke modal */}
                {adminRevokeModal.open && (
                    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" data-testid="manual-otp-admin-revoke-modal">
                        <div className="bg-white rounded-xl max-w-md w-full p-6 space-y-4">
                            <h3 className="font-semibold text-red-700">Revoke Attendance?</h3>
                            <p className="text-xs text-[#6b7280]">
                                Admin direct action. Attendance flips to NOT VERIFIED immediately.
                                The original OTP stays valid for same-booking re-verification per policy.
                                A separate audit event is recorded.
                            </p>
                            <textarea value={adminRevokeModal.remarks}
                                onChange={e => setAdminRevokeModal({ ...adminRevokeModal, remarks: e.target.value })}
                                data-testid="manual-otp-admin-revoke-remarks"
                                placeholder="Optional remarks for audit log…" rows={3}
                                className="w-full border border-[#e5e3d8] rounded-lg px-3 py-2 text-sm" />
                            <div className="flex justify-end gap-2">
                                <button onClick={() => setAdminRevokeModal({ open: false, remarks: '' })}
                                    data-testid="manual-otp-admin-revoke-cancel"
                                    className="px-3 py-2 rounded-lg border border-[#e5e3d8] text-sm">Cancel</button>
                                <button onClick={submitAdminRevoke}
                                    data-testid="manual-otp-admin-revoke-confirm"
                                    className="px-3 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm">
                                    Confirm Revoke
                                </button>
                            </div>
                        </div>
                    </div>
                )}
                {/* iter163 — Walk-in modal */}
                {walkInModal.open && (
                    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" data-testid="manual-otp-walkin-modal">
                        <div className="bg-white rounded-xl max-w-md w-full p-6 space-y-4">
                            <h3 className="font-semibold text-[#1a2332]">Schedule &amp; Verify Walk-in</h3>
                            <p className="text-xs text-[#6b7280]">Today's IST slot will be auto-assigned (10:00–17:00, 30-min granularity). You must confirm physical presence.</p>
                            <label className="flex items-start gap-2 text-sm">
                                <input type="checkbox" checked={walkInModal.presenceConfirmed}
                                    onChange={e => setWalkInModal({ ...walkInModal, presenceConfirmed: e.target.checked })}
                                    data-testid="manual-otp-walkin-presence" className="mt-1" />
                                <span>Candidate is physically present at Chennai office right now.</span>
                            </label>
                            {applicant?.chennai_availability_latest === false && (
                                <label className="flex items-start gap-2 text-sm">
                                    <input type="checkbox" checked={walkInModal.chennaiConfirmed}
                                        onChange={e => setWalkInModal({ ...walkInModal, chennaiConfirmed: e.target.checked })}
                                        data-testid="manual-otp-walkin-chennai" className="mt-1" />
                                    <span>Candidate previously answered NO to Chennai availability — confirm the new YES.</span>
                                </label>
                            )}
                            <div className="flex justify-end gap-2 pt-2">
                                <button onClick={() => setWalkInModal({ open: false })}
                                    data-testid="manual-otp-walkin-cancel"
                                    className="px-3 py-2 rounded-lg border border-[#e5e3d8] text-sm">Cancel</button>
                                <button onClick={submitWalkIn} disabled={!walkInModal.presenceConfirmed}
                                    data-testid="manual-otp-walkin-submit"
                                    className="px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm disabled:opacity-50">Confirm Walk-in</button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Post-verify success */}
                {verified && (
                    <div className="bg-[#fffdf7] border border-emerald-200 rounded-2xl overflow-hidden" data-testid="manual-otp-result">
                        <div className="px-5 py-3 bg-emerald-50 border-b border-emerald-200 flex items-center gap-2">
                            <CheckCircle size={18} weight="fill" className="text-emerald-700" />
                            <h2 className="text-sm font-semibold text-emerald-800">Applicant Verified</h2>
                        </div>
                        <table className="w-full text-sm">
                            <tbody>
                                {[
                                    ['Name', verified.name],
                                    ['Phone', verified.phone],
                                    ['Email', verified.email],
                                    ['Job Role', verified.job_role],
                                    ['College Type', verified.college_type],
                                    ['Source (HR Team)', verified.source],
                                    ['Schedule Date', fmtDate(verified.schedule_date)],
                                    ['Schedule Time', fmtTime(verified.schedule_time)],
                                    ['OTP', verified.otp],
                                ].map(([k, v]) => (
                                    <tr key={k} className="border-b border-[#ece9dc] last:border-b-0">
                                        <td className="px-5 py-2.5 text-[#6b7280] w-44">{k}</td>
                                        <td className="px-5 py-2.5 text-[#1a2332] font-medium">{v || '—'}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </main>
        </div>
    );
}
