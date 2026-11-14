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
 *
 * Enhancement 1 UI/UX correction (2026-02-14):
 *   - Larger, more readable typography (text-base body, text-lg section titles)
 *   - Keyboard-accessible radio tiles with visible focus rings
 *   - Explicit context-sensitive action buttons:
 *       • "Save & Keep My Application Active" when candidate picks NO + keep
 *         (idempotent — if NO is already persisted it just confirms the state)
 *       • "Confirm Withdrawal" (via existing withdraw flow)
 *   - Clear "keep-active" thank-you screen so the NO path no longer dead-ends.
 */
import { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import { formatDateDDMMYYYY as fmtDate, formatTime12H as fmtTime } from '../utils/dateFormat';

const API = process.env.REACT_APP_BACKEND_URL;

const OTHER_MAX = 50;

function Section({ children, testid }) {
    return (
        <div className="space-y-3" data-testid={testid}>{children}</div>
    );
}

function Radio({ name, value, checked, onChange, testid, children }) {
    return (
        <label
            className={`flex items-start gap-2.5 cursor-pointer rounded-lg border px-3 py-2.5 transition
                focus-within:ring-2 focus-within:ring-[#2563eb] focus-within:ring-offset-1
                ${checked ? 'border-[#2563eb] bg-[#eff6ff]' : 'border-gray-300 bg-white hover:border-[#2563eb]'}`}>
            <input type="radio" name={name} value={value} checked={checked} onChange={onChange}
                   data-testid={testid}
                   className="mt-0.5 accent-[#2563eb] w-4 h-4 focus:outline-none" />
            <span className="text-sm text-gray-900 leading-snug">{children}</span>
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
    // Enhancement 1 UI fix — explicit "keep active" acknowledgement screen
    // (when the NO answer has been persisted AND the candidate confirmed
    // they do not want to withdraw).
    const [keptActive, setKeptActive] = useState(false);

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
        if (keptActive) return 'kept_active';
        if (candidate.withdrawn) return 'reactivate';
        // Fresh / unanswered or user is re-answering
        if (choice === null) {
            return 'ask';
        }
        if (choice === true) return 'allow_book';
        // choice === false → reason + withdrawal ask
        if (withdrawDecision === true) return 'withdraw';
        return 'no_reason';
    }, [candidate, config, choice, withdrawDecision, thanks, keptActive]);

    // Hand booking off to parent once YES is persisted server-side.
    useEffect(() => {
        if (mode === 'allow_book' && candidate?.latest_availability === true) {
            onAllowBooking?.();
        }
    }, [mode, candidate?.latest_availability, onAllowBooking]);

    if (loading) return <div className="text-center text-gray-500 py-10 text-base">Loading…</div>;
    if (error) return <div className="text-center text-red-500 py-10 text-base" data-testid="availability-error">{error}</div>;

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

    // Enhancement 1 UI fix — the NO + "keep my application active" branch
    // used to dead-end at a passive green banner. The explicit button below
    // persists the (already stored) NO decision as idempotent "keep active"
    // acknowledgement: if availability=false and a reason is already on the
    // doc we just confirm the state (no second POST, no duplicate audit event);
    // otherwise we re-send the saved selection with the stable nonce so the
    // backend upsert is a no-op on retry.
    const saveKeepActive = async () => {
        if (submitting) return;
        // Guard: reason and availability=false must already be persisted.
        if (candidate.latest_availability === false && candidate.latest_reason_id) {
            setKeptActive(true);
            return;
        }
        // Fallback (should not normally run — reason is submitted before the
        // withdraw radios appear). Preserve the audit trail by routing through
        // the same endpoint with the same nonce so a retry is idempotent.
        setSubmitting(true);
        try {
            const body = { availability: false, submission_nonce: availNonce };
            if (reasonId) {
                body.reason_id = reasonId;
                if (reasonId === 'other') body.other_text = otherText.trim();
            }
            const r = await axios.post(`${API}/api/pub/availability/${token}`, body);
            setCandidate(r.data.candidate);
            setKeptActive(true);
        } catch (e) {
            alert(e.response?.data?.detail || 'Could not save your response.');
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
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"
                 data-testid="prior-answer-banner">
                <b>Your previous answer:</b> <span data-testid="prior-answer-text">{label}</span>
            </div>
        );
    };

    const activeReasons = (config.reason_options || []).filter(r => r.active !== false);
    const primaryBtn = "w-full py-2.5 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold rounded-lg text-sm tracking-wide disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#2563eb] focus-visible:ring-offset-2";
    const secondaryBtn = "flex-1 py-2.5 border border-gray-300 bg-white hover:bg-gray-50 text-gray-900 font-semibold rounded-lg text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 focus-visible:ring-offset-2";

    // ===== renderers =====
    if (mode === 'thanks') {
        return (
            <Section testid="availability-thanks">
                <h2 className="text-lg font-bold text-gray-900">Thank you.</h2>
                <p className="text-gray-700 text-sm leading-relaxed">{thanks}</p>
            </Section>
        );
    }

    if (mode === 'kept_active') {
        return (
            <Section testid="availability-kept-active">
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-emerald-900"
                     data-testid="kept-active-banner">
                    <h2 className="text-base font-bold mb-0.5">Your application is active.</h2>
                    <p className="text-sm leading-relaxed">
                        We've saved your answer. You can return to this link later if your plans change.
                    </p>
                    {candidate.has_booking && candidate.booking && (
                        <div className="mt-2 text-emerald-950 text-xs">
                            <div className="font-semibold">Current booking (preserved):</div>
                            <div data-testid="kept-active-booked-date">Date: {fmtDate(candidate.booking.schedule_date)}</div>
                            <div data-testid="kept-active-booked-time">Time: {fmtTime(candidate.booking.schedule_time)}</div>
                        </div>
                    )}
                </div>
                <button
                    type="button"
                    onClick={() => { setKeptActive(false); setChoice(null); setReasonId(''); setOtherText(''); setWithdrawDecision(null); }}
                    data-testid="kept-active-change"
                    className="text-xs text-[#2563eb] hover:underline font-semibold">
                    ← Change my answer
                </button>
            </Section>
        );
    }

    if (mode === 'reactivate') {
        return (
            <Section testid="availability-reactivate">
                <h2 className="text-base font-bold text-gray-900">{config.reactivation_question}</h2>
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
                              className="w-full border border-gray-300 rounded-lg p-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#2563eb]"/>
                )}
                <button onClick={submitReactivation} disabled={submitting} data-testid="react-submit"
                        className={primaryBtn}>
                    {submitting ? 'Processing…' : 'Submit'}
                </button>
            </Section>
        );
    }

    if (mode === 'withdraw') {
        return (
            <Section testid="availability-withdraw">
                <p className="text-gray-800 text-sm leading-relaxed" data-testid="withdraw-explanation">
                    {config.withdraw_explanation}
                </p>
                <label className="flex items-start gap-2.5 text-xs text-gray-800 bg-gray-50 border border-gray-200 rounded-lg p-3 focus-within:ring-2 focus-within:ring-[#2563eb]">
                    <input type="checkbox" checked={withdrawChecked} onChange={e => setWithdrawChecked(e.target.checked)}
                           data-testid="withdraw-confirm-check" className="mt-0.5 accent-[#2563eb] w-4 h-4"/>
                    <span className="leading-relaxed">{config.withdraw_confirm_check}</span>
                </label>
                <div className="flex gap-2.5">
                    <button onClick={() => setWithdrawDecision(null)}
                            className={secondaryBtn}
                            data-testid="withdraw-back">Back</button>
                    <button onClick={submitWithdraw} disabled={!withdrawChecked || submitting}
                            data-testid="withdraw-confirm-btn"
                            className="flex-1 py-2.5 bg-red-600 hover:bg-red-700 text-white font-bold rounded-lg text-sm tracking-wide disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 focus-visible:ring-offset-2">
                        {submitting ? 'Processing…' : (config.withdraw_button_label || 'Confirm Withdrawal')}
                    </button>
                </div>
            </Section>
        );
    }

    if (mode === 'no_reason') {
        const reasonAlreadySaved =
            candidate.latest_availability === false && !!candidate.latest_reason_id;
        return (
            <Section testid="availability-no-reason">
                {priorBanner()}
                <div className="flex items-center justify-between gap-3">
                    <p className="text-gray-900 text-sm font-semibold">{config.reason_question}</p>
                    <button type="button" onClick={() => { setChoice(null); setReasonId(''); setOtherText(''); setWithdrawDecision(null); }}
                            data-testid="reason-back"
                            className="text-xs text-[#2563eb] hover:underline font-semibold whitespace-nowrap">← Change my answer</button>
                </div>
                <select value={reasonId} onChange={e => setReasonId(e.target.value)}
                        data-testid="reason-select"
                        disabled={reasonAlreadySaved}
                        className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-[#2563eb] disabled:bg-gray-50 disabled:text-gray-600">
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
                                  disabled={reasonAlreadySaved}
                                  className="w-full border border-gray-300 rounded-lg p-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#2563eb] disabled:bg-gray-50"/>
                        <div className="text-right text-xs text-gray-500" data-testid="reason-other-counter">
                            {otherText.length}/{OTHER_MAX}
                        </div>
                    </>
                )}
                {!reasonAlreadySaved && (
                    <button onClick={() => submitAvailability(false)}
                            disabled={submitting || !reasonId || (reasonId === 'other' && !otherText.trim())}
                            data-testid="reason-submit"
                            className={primaryBtn}>
                        {submitting ? 'Saving…' : 'Submit reason'}
                    </button>
                )}

                {/* Explanation of in-person-only policy */}
                {candidate.latest_availability === false && (
                    <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-900 leading-relaxed"
                         data-testid="inperson-only-explanation">
                        {config.explanation_text}
                    </div>
                )}

                {/* Withdrawal question */}
                {candidate.latest_availability === false && (
                    <div className="space-y-2.5 border-t border-gray-200 pt-3" data-testid="withdraw-question-block">
                        <p className="text-gray-900 text-sm font-semibold">{config.withdraw_question}</p>
                        <div className="space-y-2">
                            <Radio name="wd" value="yes" checked={withdrawDecision === true}
                                   onChange={() => setWithdrawDecision(true)} testid="wd-yes">
                                {config.withdraw_yes_label}
                            </Radio>
                            <Radio name="wd" value="no" checked={withdrawDecision === false}
                                   onChange={() => setWithdrawDecision(false)} testid="wd-no">
                                {config.withdraw_no_label}
                            </Radio>
                        </div>

                        {/* Enhancement 1 fix — explicit action button after NO + keep-active. */}
                        {withdrawDecision === false && (
                            <button onClick={saveKeepActive}
                                    disabled={submitting}
                                    data-testid="keep-active-btn"
                                    className={primaryBtn}>
                                {submitting ? 'Saving…' : 'Save & Keep My Application Active'}
                            </button>
                        )}

                        {withdrawDecision === false && candidate.has_booking && (
                            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 leading-relaxed"
                                 data-testid="booked-no-notice">
                                {config.booked_no_notice}
                                {bookedDetails && candidate.booking && (
                                    <div className="mt-1.5 text-amber-950">
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
            <h2 className="text-base font-bold text-gray-900 leading-snug" data-testid="avail-main-q">{config.main_question}</h2>
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
                    className={primaryBtn}>
                {submitting ? 'Saving…' : 'Continue'}
            </button>
        </Section>
    );
}
