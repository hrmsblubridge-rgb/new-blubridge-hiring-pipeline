/**
 * iter163 — OTP Correction Requests (admin-only)
 * Lists pending + historical requests and lets a top-level admin approve/reject.
 */
import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { toast } from 'sonner';
import { ArrowLeft, Check, X } from '@phosphor-icons/react';

const API = process.env.REACT_APP_BACKEND_URL;

export default function OtpCorrectionRequests() {
    const navigate = useNavigate();
    const [rows, setRows] = useState([]);
    const [loading, setLoading] = useState(true);
    const [tab, setTab] = useState('PENDING');
    const [decisionModal, setDecisionModal] = useState(null); // { row, action, remarks }

    const fetchRows = useCallback(async () => {
        setLoading(true);
        try {
            const r = await axios.get(`${API}/api/bb/manual-otp/reversal-requests`,
                { params: { status: tab }, withCredentials: true });
            setRows(r.data.requests || []);
        } catch (err) {
            toast.error(err?.response?.data?.detail || 'Failed to load');
            setRows([]);
        }
        setLoading(false);
    }, [tab]);

    useEffect(() => { fetchRows(); }, [fetchRows]);

    const submitDecision = async () => {
        if (!decisionModal) return;
        const { row, action, remarks } = decisionModal;
        try {
            await axios.post(
                `${API}/api/bb/manual-otp/reversal-requests/${row.id}/${action}`,
                { remarks: remarks || '' },
                { withCredentials: true },
            );
            toast.success(`Request ${action}d`);
            setDecisionModal(null);
            fetchRows();
        } catch (err) { toast.error(err?.response?.data?.detail || 'Decision failed'); }
    };

    return (
        <div className="min-h-screen p-6" data-testid="otp-correction-requests-page">
            <button onClick={() => navigate(-1)} className="mb-4 flex items-center gap-2 text-sm text-[#6b7280]" data-testid="ocr-back">
                <ArrowLeft size={16} /> Back
            </button>
            <h1 className="text-2xl font-bold text-[#1a2332]">OTP Verification Correction Requests</h1>
            <div className="flex gap-2 mt-4">
                {['PENDING', 'APPROVED', 'REJECTED', 'OBSOLETE'].map(t => (
                    <button key={t} onClick={() => setTab(t)}
                        data-testid={`ocr-tab-${t.toLowerCase()}`}
                        className={`px-3 py-1.5 rounded-lg text-sm ${tab === t ? 'bg-[#1a2332] text-white' : 'bg-white border border-[#e5e3d8]'}`}>
                        {t}
                    </button>
                ))}
            </div>
            <div className="mt-4 bg-white border border-[#e5e3d8] rounded-xl overflow-hidden">
                {loading ? <div className="p-6 text-sm text-[#6b7280]">Loading…</div> : rows.length === 0 ? (
                    <div className="p-6 text-sm text-[#6b7280]" data-testid="ocr-empty">No {tab.toLowerCase()} requests.</div>
                ) : (
                    <table className="w-full text-sm">
                        <thead className="bg-[#faf9f1] text-[#6b7280] text-xs uppercase tracking-wider">
                            <tr>
                                <th className="px-4 py-2 text-left">Registration</th>
                                <th className="px-4 py-2 text-left">Reason</th>
                                <th className="px-4 py-2 text-left">Requested By</th>
                                <th className="px-4 py-2 text-left">Requested At</th>
                                <th className="px-4 py-2 text-left">Status</th>
                                <th className="px-4 py-2 text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows.map(r => (
                                <tr key={r.id} data-testid={`ocr-row-${r.id}`} className="border-t border-[#ece9dc]">
                                    <td className="px-4 py-2 font-mono text-xs">{r.registration_id}</td>
                                    <td className="px-4 py-2">{r.reason_code}{r.reason_text ? ` — ${r.reason_text}` : ''}</td>
                                    <td className="px-4 py-2">{r.requested_by_username}</td>
                                    <td className="px-4 py-2 text-xs">{r.requested_at}</td>
                                    <td className="px-4 py-2">
                                        <span className="px-2 py-0.5 rounded bg-[#faf9f1] border border-[#e5e3d8] text-xs">{r.status}</span>
                                    </td>
                                    <td className="px-4 py-2 text-right">
                                        {r.status === 'PENDING' && (
                                            <div className="flex gap-2 justify-end">
                                                <button onClick={() => setDecisionModal({ row: r, action: 'approve', remarks: '' })}
                                                    data-testid={`ocr-approve-${r.id}`}
                                                    className="px-2 py-1 rounded bg-emerald-600 text-white text-xs flex items-center gap-1"><Check size={12} />Approve</button>
                                                <button onClick={() => setDecisionModal({ row: r, action: 'reject', remarks: '' })}
                                                    data-testid={`ocr-reject-${r.id}`}
                                                    className="px-2 py-1 rounded bg-red-600 text-white text-xs flex items-center gap-1"><X size={12} />Reject</button>
                                            </div>
                                        )}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}
            </div>
            {decisionModal && (
                <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" data-testid="ocr-decision-modal">
                    <div className="bg-white rounded-xl max-w-md w-full p-6 space-y-4">
                        <h3 className="font-semibold text-[#1a2332] capitalize">{decisionModal.action} Correction Request</h3>
                        <textarea value={decisionModal.remarks}
                            onChange={e => setDecisionModal({ ...decisionModal, remarks: e.target.value })}
                            data-testid="ocr-remarks"
                            placeholder="Optional admin remarks…" rows={3}
                            className="w-full border border-[#e5e3d8] rounded-lg px-3 py-2 text-sm" />
                        <div className="flex justify-end gap-2">
                            <button onClick={() => setDecisionModal(null)} data-testid="ocr-decision-cancel"
                                className="px-3 py-2 rounded-lg border border-[#e5e3d8] text-sm">Cancel</button>
                            <button onClick={submitDecision} data-testid="ocr-decision-confirm"
                                className={`px-3 py-2 rounded-lg text-white text-sm ${decisionModal.action === 'approve' ? 'bg-emerald-600' : 'bg-red-600'}`}>
                                Confirm {decisionModal.action}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
