/**
 * Enhancement 1 — Chennai in-person availability gate.
 *
 * Wraps the existing /schedule-interview/:token booking page so that every
 * candidate (new, booked, rescheduling or returning) must answer the
 * Chennai in-person availability question before they can pick a slot.
 *
 * The component is purely presentational over three states:
 *   mode === 'ask'          → show the YES/NO question (and prior answer if any)
 *   mode === 'no_reason'    → NO selected; show reason dropdown + withdrawal ask
 *   mode === 'withdraw'     → withdrawal confirmation checkbox + button
 *   mode === 'reactivate'   → previously withdrawn candidate
 *   mode === 'allow_book'   → YES confirmed; parent renders the existing picker
 *   mode === 'booked_no'    → latest NO on a booked candidate; show read-only
 *   mode === 'withdrawn'    → confirmation screen after a successful withdraw
 */
import { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import { formatDateDDMMYYYY as fmtDate, formatTime12H as fmtTime } from '../utils/dateFormat';

const API = process.env.REACT_APP_BACKEND_URL;

const OTHER_MAX = 50;

function Section({ children, testid }) {
    return (
        <div className="space-y-5" data-testid={testid}>{children}</div>
    );
}

function Radio({ name, value, checked, onChange, testid, children }) {
    return (
        <label className="flex items-start gap-3 cursor-pointer rounded-lg border border-gray-200 bg-white px-4 py-3 hover:border-blue-400">
            <input type="radio" name={name} value={value} checked={checked} onChange={onChange}
                   data-testid={testid} className="mt-1 accent-blue-600 w-4 h-4" />
            <span className="text-sm text-gray-800">{children}</span>
        </label>
    );
}

export default function AvailabilityGate({ token, onAllowBooking, bookedDetails }) {
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [config, setConfig] = useState(null);
    const [candidate, setCandidate] = useState(null);

    // Local wizard state (reset whenever the token changes).
    const [choice, setChoice] = useState(null);       // true | false | null
    const [reasonId, setReasonId] = useState('');
    const [otherText, setOtherText] = useState('');
    const [withdrawDecision, setWithdrawDecision] = useState(null); // true | false | null
    const [withdrawChecked, setWithdrawChecked] = useState(false);
    const [reactChoice, setReactChoice] = useState('');  // "yes" | "no" | "other"
    const [reactOther, setReactOther] = useState('');
    const [submitting, setSubmitting] = useState(false);
    // iter65 — Idempotency: one nonce per intended ACTION. We generate a new
    // nonce whenever a fresh action starts (first POST attempt). Retries of
    // the SAME action after a network error reuse the same nonce.
    const newNonce = () => (crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`);
    const [availNonce, setAvailNonce] = useState(newNonce());
    const [withdrawNonce, setWithdrawNonce] = useState(newNonce());
    const [reactNonce, setReactNonce] = useState(newNonce());
    const [thanks, setThanks] = useState('');

    const load = () => {
        setLoading(true); setError('');
        axios.get(`${API}/api/pub/availability/${token}`)
            .then(r => { setConfig(r.data.config); setCandidate(r.data.candidate); })
            .catch(e => setError(e.response?.data?.detail || 'Invalid or expired link'))
            .finally(() => setLoading(false));
    };
    useEffect(load, [token]);

    const mode = useMemo(() => {
        if (!candidate || !config) return 'loading';
        if (thanks) return 'thanks';
        if (candidate.withdrawn) return 'reactivate';
        // Fresh / unanswered or user is re-answering
        if (choice === null) {
            return 'ask';
        }
        if (choice === true) return 'allow_book';
        // choice === false → reason + withdrawal ask
        if (withdrawDecision === true) return 'withdraw';
        return 'no_reason';
    }, [candidate, config, choice, withdrawDecision, thanks]);

    // Hand booking off to parent once YES is persisted server-side.
    useEffect(() => {
        if (mode === 'allow_book' && candidate?.latest_availability === true) {
            onAllowBooking?.();
        }
    }, [mode, candidate?.latest_availability, onAllowBooking]);

    if (loading) return <div className="text-center text-gray-500 py-10">Loading…</div>;
    if (error) return <div className="text-center text-red-500 py-10" data-testid="availability-error">{error}</div>;

    // ----- actions -----
    const submitAvailability = async (availability) => {
        if (submitting) return;  // guard against double-click
        setSubmitting(true);
        try {
            const body = { availability, submission_nonce: availNonce };
            if (availability === false) {
                if (!reasonId) { alert('Please select a reason.'); setSubmitting(false); return; }
                body.reason_id = reasonId;
                if (reasonId === 'other') body.other_text = otherText.trim();
            }
            const r = await axios.post(`${API}/api/pub/availability/${token}`, body);
            setCandidate(r.data.candidate);
            // Rotate the nonce only after the server confirms — a new intent
            // (different YES/NO or a new reason) becomes a new submission.
            setAvailNonce(newNonce());
        } catch (e) {
            alert(e.response?.data?.detail || 'Could not save your response.');
        } finally { setSubmitting(false); }
    };

    const submitWithdraw = async () => {
        if (submitting) return;
        setSubmitting(true);
        try {
            const r = await axios.post(`${API}/api/pub/availability/${token}/withdraw`,
                                       { confirmed: true, submission_nonce: withdrawNonce });
            setCandidate(r.data.candidate);
            setThanks(config.withdraw_thankyou);
            setWithdrawNonce(newNonce());
        } catch (e) {
            alert(e.response?.data?.detail || 'Could not withdraw.');
        } finally { setSubmitting(false); }
    };

    const submitReactivation = async () => {
        if (!reactChoice) { alert('Please select an option.'); return; }
        if (submitting) return;
        setSubmitting(true);
        try {
            const body = { choice: reactChoice, submission_nonce: reactNonce };
            if (reactChoice === 'other') body.other_text = reactOther.trim();
            const r = await axios.post(`${API}/api/pub/availability/${token}/reactivate`, body);
            setCandidate(r.data.candidate);
            setReactNonce(newNonce());
            if (reactChoice === 'no') setThanks(config.reactivation_no_confirmation);
            else if (reactChoice === 'other') setThanks(config.reactivation_other_confirmation);
            else {
                // YES: backend already set chennai_availability_latest=true and cleared
                // withdrawn_by_candidate. Hand off directly to the slot picker rather
                // than re-asking the main question.
                setChoice(true);
                setThanks('');
                onAllowBooking?.();
            }
        } catch (e) {
            alert(e.response?.data?.detail || 'Could not process your request.');
        } finally { setSubmitting(false); }
    };

    // Prior answer banner — shown above the question on re-visit.
    const priorBanner = () => {
        if (candidate.latest_availability === null) return null;
        const label = candidate.latest_availability
            ? config.yes_label
            : (config.no_label + (candidate.latest_reason_text ? ` — ${candidate.latest_reason_text}` : ''));
        return (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900"
                 data-testid="prior-answer-banner">
                <b>Your previous answer:</b> <span data-testid="prior-answer-text">{label}</span>
            </div>
        );
    };

    const activeReasons = (config.reason_options || []).filter(r => r.active !== false);

    // ===== renderers =====
    if (mode === 'thanks') {
        return (
            <Section testid="availability-thanks">
                <h2 className="text-xl font-bold text-gray-900">Thank you.</h2>
                <p className="text-gray-700 text-sm">{thanks}</p>
            </Section>
        );
    }

    if (mode === 'reactivate') {
        return (
            <Section testid="availability-reactivate">
                <h2 className="text-xl font-bold text-gray-900">{config.reactivation_question}</h2>
                <div className="space-y-2">
                    <Radio name="react" value="yes" checked={reactChoice === 'yes'}
                           onChange={() => setReactChoice('yes')} testid="react-yes">
                        {config.reactivation_yes_label}
                    </Radio>
                    <Radio name="react" value="no" checked={reactChoice === 'no'}
                           onChange={() => setReactChoice('no')} testid="react-no">
                        {config.reactivation_no_label}
                    </Radio>
                    <Radio name="react" value="other" checked={reactChoice === 'other'}
                           onChange={() => setReactChoice('other')} testid="react-other">
                        {config.reactivation_other_label}
                    </Radio>
                </div>
                {reactChoice === 'other' && (
                    <textarea value={reactOther} onChange={e => setReactOther(e.target.value)}
                              placeholder={config.reactivation_other_placeholder}
                              data-testid="react-other-text" maxLength={500}
                              className="w-full border border-gray-300 rounded p-2 text-sm"/>
                )}
                <button onClick={submitReactivation} disabled={submitting} data-testid="react-submit"
                        className="w-full py-3 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold rounded-lg disabled:opacity-50">
                    {submitting ? 'Processing…' : 'Submit'}
                </button>
            </Section>
        );
    }

    if (mode === 'withdraw') {
        return (
            <Section testid="availability-withdraw">
                <p className="text-gray-700 text-sm" data-testid="withdraw-explanation">
                    {config.withdraw_explanation}
                </p>
                <label className="flex items-start gap-3 text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded p-3">
                    <input type="checkbox" checked={withdrawChecked} onChange={e => setWithdrawChecked(e.target.checked)}
                           data-testid="withdraw-confirm-check" className="mt-0.5 accent-blue-600"/>
                    <span>{config.withdraw_confirm_check}</span>
                </label>
                <div className="flex gap-3">
                    <button onClick={() => setWithdrawDecision(null)}
                            className="flex-1 py-3 border border-gray-300 rounded-lg"
                            data-testid="withdraw-back">Back</button>
                    <button onClick={submitWithdraw} disabled={!withdrawChecked || submitting}
                            data-testid="withdraw-confirm-btn"
                            className="flex-1 py-3 bg-red-600 hover:bg-red-700 text-white font-bold rounded-lg disabled:opacity-50">
                        {submitting ? 'Processing…' : config.withdraw_button_label}
                    </button>
                </div>
            </Section>
        );
    }

    if (mode === 'no_reason') {
        // Confirmation for "NO + keep application active": once the reason
        // is saved AND user picked withdraw=false we show a clear thank-you
        // state so there is no dead-end (reported in iter64 testing).
        const noWithdrawConfirmed =
            candidate.latest_availability === false &&
            withdrawDecision === false &&
            candidate.latest_reason_id;
        return (
            <Section testid="availability-no-reason">
                {priorBanner()}
                <div className="flex items-center justify-between">
                    <p className="text-gray-800 text-sm font-medium">{config.reason_question}</p>
                    <button type="button" onClick={() => { setChoice(null); setReasonId(''); setOtherText(''); setWithdrawDecision(null); }}
                            data-testid="reason-back"
                            className="text-xs text-blue-600 hover:underline">← Change my answer</button>
                </div>
                <select value={reasonId} onChange={e => setReasonId(e.target.value)}
                        data-testid="reason-select"
                        className="w-full border border-gray-300 rounded p-2 text-sm">
                    <option value="">— select one —</option>
                    {activeReasons.map(r => (
                        <option key={r.id} value={r.id} data-testid={`reason-opt-${r.id}`}>{r.label}</option>
                    ))}
                </select>
                {reasonId === 'other' && (
                    <>
                        <textarea value={otherText}
                                  onChange={e => setOtherText(e.target.value.slice(0, OTHER_MAX))}
                                  placeholder={config.other_label}
                                  data-testid="reason-other-text"
                                  className="w-full border border-gray-300 rounded p-2 text-sm"/>
                        <div className="text-right text-xs text-gray-500" data-testid="reason-other-counter">
                            {otherText.length}/{OTHER_MAX}
                        </div>
                    </>
                )}
                {!candidate.latest_reason_id && (
                    <button onClick={() => submitAvailability(false)}
                            disabled={submitting || !reasonId || (reasonId === 'other' && !otherText.trim())}
                            data-testid="reason-submit"
                            className="w-full py-3 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold rounded-lg disabled:opacity-50">
                        {submitting ? 'Saving…' : 'Submit reason'}
                    </button>
                )}

                {/* Explanation of in-person-only policy */}
                {candidate.latest_availability === false && (
                    <div className="rounded-md border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900"
                         data-testid="inperson-only-explanation">
                        {config.explanation_text}
                    </div>
                )}

                {/* Withdrawal question */}
                {candidate.latest_availability === false && (
                    <div className="space-y-3 border-t pt-4" data-testid="withdraw-question-block">
                        <p className="text-gray-800 text-sm font-medium">{config.withdraw_question}</p>
                        <Radio name="wd" value="yes" checked={withdrawDecision === true}
                               onChange={() => setWithdrawDecision(true)} testid="wd-yes">
                            {config.withdraw_yes_label}
                        </Radio>
                        <Radio name="wd" value="no" checked={withdrawDecision === false}
                               onChange={() => setWithdrawDecision(false)} testid="wd-no">
                            {config.withdraw_no_label}
                        </Radio>
                        {noWithdrawConfirmed && (
                            <div className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900"
                                 data-testid="keep-active-confirmation">
                                Your application remains active. You can return to this link later to update your answer.
                                {candidate.has_booking && candidate.booking && (
                                    <div className="mt-2 text-emerald-950">
                                        <div><b>Current booking (preserved):</b></div>
                                        <div data-testid="booked-no-date">Date: {fmtDate(candidate.booking.schedule_date)}</div>
                                        <div data-testid="booked-no-time">Time: {fmtTime(candidate.booking.schedule_time)}</div>
                                    </div>
                                )}
                            </div>
                        )}
                        {withdrawDecision === false && !noWithdrawConfirmed && candidate.has_booking && (
                            <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900"
                                 data-testid="booked-no-notice">
                                {config.booked_no_notice}
                                {bookedDetails && candidate.booking && (
                                    <div className="mt-2 text-amber-950">
                                        <div><b>Current booking:</b></div>
                                        <div data-testid="booked-no-date">Date: {fmtDate(candidate.booking.schedule_date)}</div>
                                        <div data-testid="booked-no-time">Time: {fmtTime(candidate.booking.schedule_time)}</div>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </Section>
        );
    }

    // mode === 'ask'  (default)
    return (
        <Section testid="availability-ask">
            {priorBanner()}
            <h2 className="text-xl font-bold text-gray-900" data-testid="avail-main-q">{config.main_question}</h2>
            <div className="space-y-2">
                <Radio name="avail" value="yes" checked={choice === true}
                       onChange={() => setChoice(true)} testid="avail-yes">
                    {config.yes_label}
                </Radio>
                <Radio name="avail" value="no" checked={choice === false}
                       onChange={() => setChoice(false)} testid="avail-no">
                    {config.no_label}
                </Radio>
            </div>
            <button onClick={() => {
                if (choice === true) submitAvailability(true);
                // choice === false → wizard moves to no_reason via render; no immediate POST
            }}
                    disabled={choice === null || (choice === true && submitting)}
                    data-testid="avail-continue"
                    className="w-full py-3 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold rounded-lg disabled:opacity-50">
                {submitting ? 'Saving…' : 'Continue'}
            </button>
        </Section>
    );
}
