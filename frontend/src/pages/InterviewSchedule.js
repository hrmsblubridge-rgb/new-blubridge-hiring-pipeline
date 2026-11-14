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
        <header className="bg-[#efede5] border-b border-gray-300 py-4 px-6 flex justify-center">
            <img src="/blubridge-logo.webp" alt="Blubridge" className="h-10 sm:h-12" />
        </header>
        <div className="flex-1 flex items-start justify-center px-4 sm:px-6 py-8 sm:py-10">
            {children}
        </div>
        <footer className="py-4 text-center text-sm text-gray-500">Copyright 2026 &copy; <b>Blubridge.com</b></footer>
    </div>
);

// Enhancement 1 UI fix — compact, balanced detail row used inside the
// "Interview Details" info card. Larger label + value typography than the
// old `text-xs` labels.
const DetailRow = ({ label, value, testid }) => (
    <div className="flex flex-col sm:flex-row sm:items-baseline gap-1 sm:gap-3">
        <span className="text-[11px] sm:text-xs font-bold tracking-widest text-gray-500 uppercase sm:w-20 shrink-0">{label}</span>
        <span className="text-base text-gray-900 font-medium break-words" data-testid={testid}>{value || '—'}</span>
    </div>
);

export default function InterviewSchedule() {
    const { token } = useParams();
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
                <div className="w-full max-w-xl">
                    <div className="bg-[#fffdf7] rounded-2xl shadow-md overflow-hidden">
                        <div className="bg-[#1a2332] h-3 rounded-t-2xl"></div>
                        <div className="p-6 sm:p-10 space-y-6">
                            <h2 className="text-2xl sm:text-3xl font-bold text-[#1a2332] text-center tracking-tight" data-testid="sched-success-heading">
                                {info?.already_scheduled ? 'Your Interview Has Been Rescheduled!' : 'Your Interview Has Been Scheduled!'}
                            </h2>
                            <div className="h-px bg-gray-300"></div>
                            <p className="text-gray-800 text-base leading-relaxed">
                                Thank you for scheduling your interview with <b>Blubridge Technologies</b>.
                            </p>
                            <div className="rounded-xl border border-gray-200 bg-white px-5 py-4 space-y-2 text-base text-gray-900">
                                <div className="text-sm font-bold uppercase tracking-widest text-gray-500">Interview Details</div>
                                <div><b>Date:</b> <span data-testid="sched-success-date">{shownDate}</span></div>
                                <div><b>Time:</b> <span data-testid="sched-success-time">{shownTime}</span></div>
                                <div><b>Location:</b> <span data-testid="sched-success-location">{COMPANY_ADDRESS}</span></div>
                            </div>
                            <p className="text-gray-800 text-base leading-relaxed">
                                A confirmation email with the above details has been sent to your provided email address.
                            </p>
                            <div className="bg-[#fff9e6] border border-[#f5d76e] rounded-lg p-4 text-sm text-gray-800 leading-relaxed">
                                <b>⚠ Important:</b> Please ensure you check your <b>Spam/Junk</b> folder if you don't see our email in your inbox, and mark it as "Not Spam" to stay updated.
                            </div>
                            <p className="text-gray-900 text-base font-semibold">We look forward to meeting you in person!</p>
                            <ConsentOutcome state={consentSaved} manageToken={manageToken} />
                        </div>
                    </div>
                </div>
            </PageShell>
        );
    }

    return (
        <PageShell testid="schedule-page">
            <div className="w-full max-w-xl">
                <div className="bg-[#fffdf7] rounded-2xl shadow-md overflow-hidden">
                    <div className="bg-[#1a2332] h-3 rounded-t-2xl"></div>
                    <div className="p-6 sm:p-10 space-y-7">
                        <div className="text-center space-y-2">
                            <h2 className="text-2xl sm:text-3xl font-bold text-[#1a2332] tracking-tight">
                                {info?.already_scheduled ? 'Your Interview Details' : 'Schedule Your In-Person Interview'}
                            </h2>
                            {missed && <p className="text-red-600 text-base font-semibold" data-testid="sched-missed-banner">You have missed your interview!</p>}
                            {info?.already_scheduled && !missed && <p className="text-emerald-700 text-sm font-bold tracking-wide uppercase" data-testid="sched-already-banner">Your interview is already scheduled</p>}
                        </div>

                        <section
                            aria-labelledby="interview-info-heading"
                            data-testid="interview-info-card"
                            className="rounded-xl border border-gray-200 bg-white px-5 py-5 space-y-3">
                            <h3 id="interview-info-heading" className="text-sm font-bold uppercase tracking-widest text-gray-500">Interview Information</h3>
                            <DetailRow label="Name"  value={info?.name}  testid="sched-name" />
                            <DetailRow label="Email" value={info?.email} testid="sched-email" />
                            <DetailRow label="Phone" value={info?.phone} testid="sched-phone" />
                            {info?.already_scheduled && info?.schedule_date && (
                                <>
                                    <DetailRow label="Date" value={fmtDate(info.schedule_date)} testid="sched-current-date" />
                                    <DetailRow label="Time" value={fmtTime(info.schedule_time)} testid="sched-current-time" />
                                </>
                            )}
                        </section>

                        {/* Enhancement 1 — availability gate. Slot picker only
                            renders once the candidate persists YES. */}
                        {!bookingAllowed && (
                            <section
                                aria-labelledby="chennai-availability-heading"
                                className="rounded-xl border border-blue-200 bg-[#f8fbff] px-5 py-5 space-y-4"
                                data-testid="availability-gate-wrapper">
                                <h3 id="chennai-availability-heading" className="text-sm font-bold uppercase tracking-widest text-[#1d4ed8]">Chennai In-Person Availability</h3>
                                <AvailabilityGate token={token} onAllowBooking={onAllowBooking} bookedDetails />
                            </section>
                        )}

                        {bookingAllowed && (
                        <>
                        <section
                            aria-labelledby="pick-slot-heading"
                            data-testid="slot-picker"
                            className="rounded-xl border border-gray-200 bg-white px-5 py-5 space-y-4">
                            <h3 id="pick-slot-heading" className="text-sm font-bold uppercase tracking-widest text-gray-500">Pick your interview slot</h3>
                            <div className="space-y-1.5">
                                <label className="text-sm text-gray-700 font-semibold" htmlFor="sched-date-input">Date</label>
                                <input id="sched-date-input" type="date" value={date} min={today} onChange={e => {
                                    if (!isBlocked(e.target.value)) { setDate(e.target.value); setTime(''); }
                                    else alert('Sundays and holidays are not available');
                                }} data-testid="sched-date"
                                    className="w-full bg-white border border-gray-300 rounded-lg px-3 py-3 text-base focus:outline-none focus:ring-2 focus:ring-[#2563eb] focus:border-[#2563eb]" />
                            </div>
                            <div className="space-y-1.5">
                                <label className="text-sm text-gray-700 font-semibold" htmlFor="sched-time-input">Time</label>
                                <select id="sched-time-input" value={time} onChange={e => setTime(e.target.value)} data-testid="sched-time"
                                    className="w-full bg-white border border-gray-300 rounded-lg px-3 py-3 text-base focus:outline-none focus:ring-2 focus:ring-[#2563eb] focus:border-[#2563eb]">
                                    <option value="">Select Time</option>
                                    {TIME_SLOTS.map(t => {
                                        const disabled = isSlotDisabled(t);
                                        return <option key={t} value={t} disabled={disabled} data-testid={`sched-slot-${t.replace(/[^a-z0-9]/gi, '')}${disabled ? '-disabled' : ''}`}>{t}{disabled ? '  (past)' : ''}</option>;
                                    })}
                                </select>
                            </div>
                        </section>

                        <label className="flex items-start gap-3 text-sm text-gray-800 bg-[#f8fafc] border border-gray-200 rounded-lg p-4 focus-within:ring-2 focus-within:ring-[#2563eb]" data-testid="ai-consent-row">
                            <input type="checkbox" checked={aiConsent} onChange={e => setAiConsent(e.target.checked)} disabled={!consentInfo}
                                data-testid="ai-consent-checkbox" className="mt-0.5 accent-[#2563eb] w-4 h-4" />
                            <span className="whitespace-pre-line leading-relaxed">
                                <b className="block mb-1 text-base" data-testid="ai-consent-title">{consentInfo?.title || 'Optional AI Voice Call Consent'}</b>
                                {consentInfo?.text || 'Loading…'}
                            </span>
                        </label>

                        {info?.already_scheduled && <ConsentRenewBox token={token} />}

                        <button onClick={handleSchedule} disabled={submitting} data-testid="schedule-btn"
                            className="w-full py-3.5 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold rounded-lg text-base tracking-wide disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#2563eb] focus-visible:ring-offset-2">
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
