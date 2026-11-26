import { useState, useEffect, useMemo, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import axios from 'axios';
import AvailabilityGate from '../components/AvailabilityGate';
import { ConsentRenewBox } from '../components/ConsentRenewBox';
import { formatDateDDMMYYYY as fmtDate, formatTime12H as fmtTime } from '../utils/dateFormat';

const API = process.env.REACT_APP_BACKEND_URL;

const ConsentOutcome = ({ state, manageToken }) => (
    <div className="text-xs text-gray-600 border-t border-gray-200 pt-3 space-y-1" data-testid="ai-consent-outcome">
        {state === 'granted' && <p>You agreed to AI voice calls about this interview.</p>}
        {state === 'not_given' && <p>You did not opt in to AI voice calls. Your interview is booked either way.</p>}
        {state === 'failed' && <p className="text-amber-700" data-testid="ai-consent-failed">Your interview is booked, but your AI call preference could not be saved, so it counts as not given.</p>}
        {manageToken && (
            <p>
                <a href={`/ai-consent#t=${manageToken}`} target="_blank" rel="noreferrer noopener" className="text-[#2563eb] underline" data-testid="manage-ai-consent-link">Manage AI calling consent</a>
                <span className="text-gray-400"> — private link, valid 30 days. Please bookmark it. You can always use it to withdraw consent.</span>
            </p>
        )}
    </div>
);
const TIME_SLOTS = ['10:00 AM','10:30 AM','11:00 AM','11:30 AM','12:00 PM','12:30 PM','01:00 PM','01:30 PM','02:00 PM','02:30 PM','03:00 PM','03:30 PM','04:00 PM','04:30 PM','05:00 PM'];

function convertTo24h(t) {
    const [time, period] = t.split(' ');
    let [h, m] = time.split(':').map(Number);
    if (period === 'PM' && h !== 12) h += 12;
    if (period === 'AM' && h === 12) h = 0;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:00`;
}

// iter79 — Convert a TIME_SLOTS label ('01:00 PM') to total minutes-of-day.
function slotMinutes(label) {
    const [tm, period] = label.split(' ');
    let [h, m] = tm.split(':').map(Number);
    if (period === 'PM' && h !== 12) h += 12;
    if (period === 'AM' && h === 12) h = 0;
    return h * 60 + m;
}

function isSunday(dateStr) {
    return new Date(dateStr).getDay() === 0;
}

const PageShell = ({ children, testid }) => (
    <div className="min-h-screen bg-[#f3f1e9] flex flex-col" data-testid={testid}>
        <header className="bg-[#efede5] border-b border-gray-300 py-3 px-6 flex justify-center">
            <img src="/blubridge-logo.webp" alt="Blubridge" className="h-8 sm:h-10" />
        </header>
        <div className="flex-1 flex items-start justify-center px-4 py-5 sm:py-6">
            {children}
        </div>
        <footer className="py-3 text-center text-xs text-gray-500">Copyright 2026 &copy; <b>Blubridge.com</b></footer>
    </div>
);

// Enhancement 1 UI fix — compact single-line detail cell. Uppercase label on top,
// value underneath on one line. If the value is too long for the cell, it
// truncates with an ellipsis and the full text is available on hover (title).
const DetailCell = ({ label, value, testid, mono }) => {
    const text = value || '—';
    return (
        <div className="min-w-0">
            <div className="text-[10px] font-bold tracking-widest text-gray-500 uppercase">{label}</div>
            <div
                className={`text-[15px] text-gray-900 font-medium leading-snug whitespace-nowrap overflow-hidden text-ellipsis ${mono ? 'tabular-nums' : ''}`}
                title={text}
                data-testid={testid}>
                {text}
            </div>
        </div>
    );
};

export default function InterviewSchedule() {
    const { token } = useParams();
    // iter173 — Role-switch banner: when the candidate arrives here after
    // Case-2 role switch, we show the "previously scheduled interview
    // cancelled" notice at the top so they know the old slot is gone.
    const _search = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
    const fromSwitch = _search?.get('from_switch') === '1';
    const prevRole = _search?.get('prev_role') || '';
    const [info, setInfo] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [date, setDate] = useState('');
    const [time, setTime] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [done, setDone] = useState(false);
    const [missed, setMissed] = useState(false);
    const [aiConsent, setAiConsent] = useState(false);
    const [consentInfo, setConsentInfo] = useState(null);
    const [consentSaved, setConsentSaved] = useState(null);
    const [manageToken, setManageToken] = useState('');
    // Enhancement 1 — availability gate: booking form is hidden until the
    // candidate confirms YES to the Chennai in-person availability question.
    const [bookingAllowed, setBookingAllowed] = useState(false);
    const onAllowBooking = useCallback(() => setBookingAllowed(true), []);
    useEffect(() => {
        axios.get(`${API}/api/ai-calling/public/consent-text`).then(r => setConsentInfo(r.data)).catch(() => setConsentInfo(null));
    }, []);

    useEffect(() => {
        axios.get(`${API}/api/pub/schedule/${token}`).then(r => {
            setInfo(r.data);
            if (r.data.reschedule_count > 0 && !r.data.schedule_date) setMissed(true);
        }).catch(() => setError('Invalid or expired link')).finally(() => setLoading(false));
    }, [token]);

    const isBlocked = (dateStr) => {
        if (!dateStr) return false;
        if (isSunday(dateStr)) return true;
        return (info?.holidays || []).includes(dateStr);
    };

    // iter79 — Spec #6: When the selected date is today, disable all slots
    // whose 12-hour label is earlier than the current LOCAL system time.
    const today = new Date().toISOString().split('T')[0];
    const minSlotMinutes = useMemo(() => {
        if (!date || date !== today) return -1;
        const now = new Date();
        return now.getHours() * 60 + now.getMinutes();
    }, [date, today]);
    const isSlotDisabled = (label) => minSlotMinutes >= 0 && slotMinutes(label) <= minSlotMinutes;

    const handleSchedule = async () => {
        if (!date || !time) { alert('Please select date and time'); return; }
        if (isBlocked(date)) { alert('This date is a holiday or Sunday'); return; }
        if (isSlotDisabled(time)) { alert('Selected time slot is in the past. Please pick a future slot.'); return; }
        setSubmitting(true);
        try {
            const time24 = convertTo24h(time);
            await axios.post(`${API}/api/pub/schedule/${token}`, { date, time: time24 });
            // Booking succeeded. The optional AI-call preference is saved separately and can never undo the booking.
            try {
                const r = await axios.post(`${API}/api/ai-calling/public/booking-consent/${token}`, { granted: aiConsent, text_version: consentInfo?.version });
                setManageToken(r.data.manage_token || '');
                setConsentSaved(aiConsent ? 'granted' : 'not_given');
            } catch { setConsentSaved('failed'); }
            setDone(true);
        } catch (e) {
            const status = e.response?.status;
            const detail = e.response?.data?.detail || 'Failed';
            if (status === 409) {
                // OTP already verified — block reschedule with a clear message
                alert(detail);
                setError(detail);
            } else {
                alert(detail);
            }
        }
        finally { setSubmitting(false); }
    };

    if (loading) return <div className="min-h-screen bg-[#f3f1e9] flex items-center justify-center text-gray-500">Loading...</div>;
    if (error) return <div className="min-h-screen bg-[#f3f1e9] flex items-center justify-center text-red-500">{error}</div>;
    if (done) {
        // iter79 — Use centralized fmtDate / fmtTime helpers (dd-mm-yyyy + hh:mm AM/PM)
        const shownDate = fmtDate(date) || fmtDate(info?.schedule_date);
        const shownTime = fmtTime(time) || fmtTime(info?.schedule_time);
        const COMPANY_ADDRESS = '30, Norton Road, Mandavelipakkam, Raja Annamalai Puram, Chennai, Tamil Nadu - 600028';
        return (
            <PageShell testid="schedule-success">
                <div className="w-full max-w-2xl">
                    <div className="bg-[#fffdf7] rounded-2xl shadow-md overflow-hidden">
                        <div className="bg-[#1a2332] h-2 rounded-t-2xl"></div>
                        <div className="px-5 py-5 sm:px-7 sm:py-6 space-y-4">
                            <h2 className="text-xl sm:text-2xl font-bold text-[#1a2332] text-center tracking-tight" data-testid="sched-success-heading">
                                {info?.already_scheduled ? 'Your Interview Has Been Rescheduled!' : 'Your Interview Has Been Scheduled!'}
                            </h2>
                            <p className="text-gray-800 text-sm leading-relaxed">
                                Thank you for scheduling your interview with <b>Blubridge Technologies</b>.
                            </p>
                            <div className="rounded-xl border border-gray-200 bg-white px-4 py-3 space-y-1.5 text-sm text-gray-900">
                                <div className="text-[10px] font-bold uppercase tracking-widest text-gray-500">Interview Details</div>
                                <div><b>Date:</b> <span data-testid="sched-success-date">{shownDate}</span></div>
                                <div><b>Time:</b> <span data-testid="sched-success-time">{shownTime}</span></div>
                                <div><b>Location:</b> <span data-testid="sched-success-location">{COMPANY_ADDRESS}</span></div>
                            </div>
                            <p className="text-gray-800 text-sm leading-relaxed">
                                A confirmation email with the above details has been sent to your provided email address.
                            </p>
                            <div className="bg-[#fff9e6] border border-[#f5d76e] rounded-lg p-3 text-xs text-gray-800 leading-relaxed">
                                <b>⚠ Important:</b> Please check your <b>Spam/Junk</b> folder if you don't see our email in your inbox, and mark it as "Not Spam" to stay updated.
                            </div>
                            <p className="text-gray-900 text-sm font-semibold">We look forward to meeting you in person!</p>
                            <ConsentOutcome state={consentSaved} manageToken={manageToken} />
                        </div>
                    </div>
                </div>
            </PageShell>
        );
    }

    return (
        <PageShell testid="schedule-page">
            <div className="w-full max-w-2xl">
                <div className="bg-[#fffdf7] rounded-2xl shadow-md overflow-hidden">
                    <div className="bg-[#1a2332] h-2 rounded-t-2xl"></div>
                    <div className="px-5 py-5 sm:px-7 sm:py-6 space-y-4">
                        <div className="text-center">
                            <h2 className="text-xl sm:text-2xl font-bold text-[#1a2332] tracking-tight">
                                {info?.already_scheduled ? 'Your Interview Details' : 'Schedule Your In-Person Interview'}
                            </h2>
                            {missed && <p className="mt-1 text-red-600 text-sm font-semibold" data-testid="sched-missed-banner">You have missed your interview!</p>}
                            {info?.already_scheduled && !missed && <p className="mt-1 text-emerald-700 text-[11px] font-bold tracking-widest uppercase" data-testid="sched-already-banner">Your interview is already scheduled</p>}
                        </div>

                        <section
                            aria-labelledby="interview-info-heading"
                            data-testid="interview-info-card"
                            className="rounded-xl border border-gray-200 bg-white px-4 py-3">
                            <h3 id="interview-info-heading" className="text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-2">Interview Information</h3>
                            <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2">
                                <DetailCell label="Name"  value={info?.name}  testid="sched-name" />
                                <DetailCell label="Phone" value={info?.phone} testid="sched-phone" mono />
                                <DetailCell label="Email" value={info?.email} testid="sched-email" />
                                {info?.already_scheduled && info?.schedule_date && (
                                    <>
                                        <DetailCell label="Date" value={fmtDate(info.schedule_date)} testid="sched-current-date" mono />
                                        <DetailCell label="Time" value={fmtTime(info.schedule_time)} testid="sched-current-time" mono />
                                    </>
                                )}
                            </div>
                        </section>

                        {/* iter173 — Role-switch banner. When a candidate arrives here
                            via a successful Case-2 role switch (query string `from_switch=1`),
                            surface the "previous interview cancelled" notice at the top of
                            the scheduling form so they understand the old slot is gone and
                            they must pick a NEW one. Hidden otherwise. */}
                        {fromSwitch && (
                            <section
                                data-testid="role-switch-banner"
                                className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3.5 space-y-1"
                                aria-labelledby="role-switch-banner-heading">
                                <h3 id="role-switch-banner-heading" className="text-[10px] font-bold uppercase tracking-widest text-amber-800">Important Notice</h3>
                                <p className="text-sm text-amber-900 leading-relaxed">
                                    Your previously scheduled interview{prevRole ? <> for <b>{prevRole}</b></> : null} has been cancelled and is no longer valid. Please schedule a new interview for your selected role{info?.job_role ? <>, <b>{info.job_role}</b></> : null}.
                                </p>
                            </section>
                        )}

                        {/* Enhancement 1 — availability gate. Slot picker only
                            renders once the candidate persists YES. */}
                        {!bookingAllowed && (
                            <section
                                aria-labelledby="chennai-availability-heading"
                                className="rounded-xl border border-blue-200 bg-[#f6faff] px-4 py-3.5 space-y-3"
                                data-testid="availability-gate-wrapper">
                                <h3 id="chennai-availability-heading" className="text-[10px] font-bold uppercase tracking-widest text-[#1d4ed8]">Chennai In-Person Availability</h3>
                                <AvailabilityGate token={token} onAllowBooking={onAllowBooking} bookedDetails />
                            </section>
                        )}

                        {bookingAllowed && (
                        <>
                        <section
                            aria-labelledby="pick-slot-heading"
                            data-testid="slot-picker"
                            className="rounded-xl border border-gray-200 bg-white px-4 py-3.5 space-y-3">
                            <h3 id="pick-slot-heading" className="text-[10px] font-bold uppercase tracking-widest text-gray-500">Pick your interview slot</h3>
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                <div>
                                    <label className="text-xs text-gray-700 font-semibold" htmlFor="sched-date-input">Date</label>
                                    <input id="sched-date-input" type="date" value={date} min={today} onChange={e => {
                                        if (!isBlocked(e.target.value)) { setDate(e.target.value); setTime(''); }
                                        else alert('Sundays and holidays are not available');
                                    }} data-testid="sched-date"
                                        className="mt-1 w-full bg-white border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#2563eb] focus:border-[#2563eb]" />
                                </div>
                                <div>
                                    <label className="text-xs text-gray-700 font-semibold" htmlFor="sched-time-input">Time</label>
                                    <select id="sched-time-input" value={time} onChange={e => setTime(e.target.value)} data-testid="sched-time"
                                        className="mt-1 w-full bg-white border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#2563eb] focus:border-[#2563eb]">
                                        <option value="">Select Time</option>
                                        {TIME_SLOTS.map(t => {
                                            const disabled = isSlotDisabled(t);
                                            return <option key={t} value={t} disabled={disabled} data-testid={`sched-slot-${t.replace(/[^a-z0-9]/gi, '')}${disabled ? '-disabled' : ''}`}>{t}{disabled ? '  (past)' : ''}</option>;
                                        })}
                                    </select>
                                </div>
                            </div>
                        </section>

                        <label className="flex items-start gap-2.5 text-xs text-gray-800 bg-[#f8fafc] border border-gray-200 rounded-lg p-3 focus-within:ring-2 focus-within:ring-[#2563eb]" data-testid="ai-consent-row">
                            <input type="checkbox" checked={aiConsent} onChange={e => setAiConsent(e.target.checked)} disabled={!consentInfo}
                                data-testid="ai-consent-checkbox" className="mt-0.5 accent-[#2563eb] w-4 h-4" />
                            <span className="whitespace-pre-line leading-relaxed">
                                <b className="block mb-0.5 text-sm" data-testid="ai-consent-title">{consentInfo?.title || 'Optional AI Voice Call Consent'}</b>
                                {consentInfo?.text || 'Loading…'}
                            </span>
                        </label>

                        {info?.already_scheduled && <ConsentRenewBox token={token} />}

                        <button onClick={handleSchedule} disabled={submitting} data-testid="schedule-btn"
                            className="w-full py-3 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold rounded-lg text-sm tracking-wide disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#2563eb] focus-visible:ring-offset-2">
                            {submitting ? 'Processing...' : (info?.already_scheduled ? 'RESCHEDULE INTERVIEW' : 'SCHEDULE INTERVIEW')}
                        </button>
                        </>
                        )}
                    </div>
                </div>
            </div>
        </PageShell>
    );
}
