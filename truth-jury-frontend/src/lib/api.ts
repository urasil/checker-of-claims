/**
 * API Service for communicating with the FactTrace Debate Backend
 * 
 * Provides methods for:
 * - Streaming debate events in real-time
 * - Running synchronous debates
 * - Fetching persona information
 */

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

export interface StartDebateRequest {
    claim: string;
    truth: string;
    model?: string;
}

export interface DebateEvent {
    event_type: 'intro' | 'turn' | 'summary' | 'verdict' | 'error' | 'complete' | 'reactions';
    persona_id: string | null;
    persona_name: string | null;
    content: string;
    round_number: number | null;
    turn_index: number | null;
    is_moderator_summary: boolean;
    verdict?: JudgeVerdictFromAPI;
    reactions?: {
        jurorId: string;
        jurorName: string;
        reaction: string;
        reason?: string;
    }[];
    timestamp: string;
}

export interface JuryVote {
    personaId: string;
    verdict: 'faithful' | 'mutated' | 'ambiguous';
    reasoning: string;
}

export interface JudgeVerdictFromAPI {
    outcome: 'faithful' | 'mutated' | 'ambiguous';
    confidence: number;
    argumentsFor: string;
    argumentsAgainst: string;
    finalJudgement: string;
    juryVotes: JuryVote[];
}

export interface APIPersona {
    id: string;
    name: string;
    role: string;
    description: string;
    isJury: boolean;
}

export interface TTSRequest {
    persona_id: string;
    text: string;
}

/**
 * Stream a debate in real-time via Server-Sent Events
 * 
 * @param request - The debate request with claim and truth
 * @param onEvent - Callback for each event received
 * @param onError - Callback for errors
 * @param onComplete - Callback when the debate completes
 */
export async function streamDebate(
    request: StartDebateRequest,
    onEvent: (event: DebateEvent) => void,
    onError?: (error: Error) => void,
    onComplete?: () => void
): Promise<void> {
    try {
        const response = await fetch(`${API_BASE_URL}/debate/stream`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(request),
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) {
            throw new Error('No response body');
        }

        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();

            if (done) {
                break;
            }

            buffer += decoder.decode(value, { stream: true });

            // Process complete SSE events
            const lines = buffer.split('\n');
            buffer = lines.pop() || ''; // Keep the last incomplete line in the buffer

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    const data = line.slice(6);
                    try {
                        const event = JSON.parse(data) as DebateEvent;
                        onEvent(event);

                        if (event.event_type === 'complete') {
                            onComplete?.();
                            return;
                        }

                        if (event.event_type === 'error') {
                            onError?.(new Error(event.content));
                            return;
                        }
                    } catch (e) {
                        console.error('Failed to parse event:', e);
                    }
                }
            }
        }

        onComplete?.();
    } catch (error) {
        onError?.(error as Error);
    }
}

/**
 * Run a synchronous debate (waits for full completion)
 */
export async function runDebateSync(request: StartDebateRequest): Promise<{
    success: boolean;
    result: any;
    verdict: JudgeVerdictFromAPI;
}> {
    const response = await fetch(`${API_BASE_URL}/debate/sync`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(request),
    });

    if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
    }

    return response.json();
}

/**
 * Get available personas from the backend
 */
export async function getPersonas(): Promise<{ personas: APIPersona[] }> {
    const response = await fetch(`${API_BASE_URL}/personas`);

    if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
    }

    return response.json();
}

/**
 * Health check for the API
 */
export async function healthCheck(): Promise<boolean> {
    try {
        const response = await fetch(`${API_BASE_URL}/health`);
        return response.ok;
    } catch {
        return false;
    }
}

/**
 * Fetch TTS audio for a persona and text.
 */
export async function fetchTtsAudio(request: TTSRequest): Promise<Blob> {
    const response = await fetch(`${API_BASE_URL}/tts`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(request),
    });

    if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `TTS error! status: ${response.status}`);
    }

    return response.blob();
}
