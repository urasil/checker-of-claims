/**
 * Custom hook for managing a live debate using the backend API
 */
import { useState, useCallback, useRef } from 'react';
import { streamDebate, DebateEvent, JudgeVerdictFromAPI, fetchTtsAudio } from '@/lib/api';
import { Message, PersonaType, JudgeVerdict, Reaction } from '@/types/debate';

export type DebatePhase = 'idle' | 'intro' | 'round1' | 'moderator-summary' | 'round2' | 'verdict' | 'complete' | 'error';

export interface UseLiveDebateOptions {
    onStart?: () => void;
    onMessage?: (message: Message) => void;
    onVerdict?: (verdict: JudgeVerdict) => void;
    onComplete?: () => void;
    onError?: (error: Error) => void;
}

export interface UseLiveDebateReturn {
    messages: Message[];
    phase: DebatePhase;
    currentSpeaker: PersonaType | null;
    isTyping: boolean;
    verdict: JudgeVerdict | null;
    error: Error | null;
    isDebating: boolean;
    progress: number;
    startDebate: (claim: string, truth: string, model?: string) => void;
    stopDebate: () => void;
    reset: () => void;
}

// Map for simulating typing delays (makes the UI feel more natural)
const TYPING_DELAY_MS = 1500;
const MESSAGE_DISPLAY_DELAY_MS = 500;

export function useLiveDebate(options: UseLiveDebateOptions = {}): UseLiveDebateReturn {
    const [messages, setMessages] = useState<Message[]>([]);
    const [phase, setPhase] = useState<DebatePhase>('idle');
    const [currentSpeaker, setCurrentSpeaker] = useState<PersonaType | null>(null);
    const [isTyping, setIsTyping] = useState(false);
    const [verdict, setVerdict] = useState<JudgeVerdict | null>(null);
    const [error, setError] = useState<Error | null>(null);
    const [isDebating, setIsDebating] = useState(false);
    const [progress, setProgress] = useState(0);

    const abortControllerRef = useRef<AbortController | null>(null);
    const eventQueueRef = useRef<DebateEvent[]>([]);
    const isProcessingRef = useRef(false);
    const currentAudioRef = useRef<HTMLAudioElement | null>(null);
    const audioUrlRef = useRef<string | null>(null);
    const stopRequestedRef = useRef(false);

    const convertApiVerdictToFrontend = (apiVerdict: JudgeVerdictFromAPI): JudgeVerdict => {
        return {
            outcome: apiVerdict.outcome,
            confidence: apiVerdict.confidence,
            argumentsFor: apiVerdict.argumentsFor,
            argumentsAgainst: apiVerdict.argumentsAgainst,
            finalJudgement: apiVerdict.finalJudgement,
            juryVotes: apiVerdict.juryVotes.map(vote => ({
                personaId: vote.personaId as any,
                verdict: vote.verdict,
                reasoning: vote.reasoning,
            })),
        };
    };

    const stopAudio = useCallback(() => {
        stopRequestedRef.current = true;
        if (currentAudioRef.current) {
            currentAudioRef.current.pause();
            currentAudioRef.current = null;
        }
        if (audioUrlRef.current) {
            URL.revokeObjectURL(audioUrlRef.current);
            audioUrlRef.current = null;
        }
    }, []);

    const playTts = useCallback(async (personaId: PersonaType, text: string) => {
        const trimmed = text.trim();
        if (!trimmed || stopRequestedRef.current) return;

        try {
            const blob = await fetchTtsAudio({ persona_id: personaId, text: trimmed });
            if (stopRequestedRef.current || blob.size === 0) return;

            const url = URL.createObjectURL(blob);
            audioUrlRef.current = url;
            const audio = new Audio(url);
            currentAudioRef.current = audio;

            await new Promise<void>((resolve) => {
                const cleanup = () => {
                    if (currentAudioRef.current === audio) {
                        currentAudioRef.current = null;
                    }
                    if (audioUrlRef.current === url) {
                        URL.revokeObjectURL(url);
                        audioUrlRef.current = null;
                    }
                    resolve();
                };

                audio.addEventListener('ended', cleanup, { once: true });
                audio.addEventListener('error', cleanup, { once: true });
                audio.play().catch(cleanup);
            });
        } catch (err) {
            console.warn('TTS failed:', err);
        }
    }, []);

    const processEvent = useCallback(async (event: DebateEvent) => {
        if (stopRequestedRef.current) return;
        // Update phase based on event
        if (event.event_type === 'intro') {
            setPhase('intro');
        } else if (event.event_type === 'turn' && event.round_number === 1) {
            setPhase('round1');
        } else if (event.event_type === 'summary') {
            setPhase('moderator-summary');
        } else if (event.event_type === 'turn' && event.round_number === 2) {
            setPhase('round2');
        } else if (event.event_type === 'verdict') {
            setPhase('verdict');
        }

        // Calculate progress (approximate: 5 jurors x 2 rounds + 2 moderator messages = 12 messages)
        const totalExpectedMessages = 12;

        if (event.event_type === 'intro' || event.event_type === 'turn' || event.event_type === 'summary') {
            // Show typing indicator
            const personaId = event.persona_id as PersonaType;
            setCurrentSpeaker(personaId);
            setIsTyping(true);

            // Wait for typing effect
            await new Promise(resolve => setTimeout(resolve, TYPING_DELAY_MS));

            // Create and add message
            const message: Message = {
                id: `msg-${event.event_type}-${event.round_number || 0}-${event.turn_index || 0}-${Date.now()}`,
                personaId,
                content: event.content,
                timestamp: new Date(event.timestamp),
                reactions: [], // Backend doesn't provide reactions yet
                isModeratorSummary: event.is_moderator_summary,
            };

            setIsTyping(false);
            setCurrentSpeaker(null);
            setMessages(prev => {
                const newMessages = [...prev, message];
                setProgress((newMessages.length / totalExpectedMessages) * 100);
                return newMessages;
            });

            options.onMessage?.(message);

            await playTts(personaId, event.content);

            // Small delay between messages
            await new Promise(resolve => setTimeout(resolve, MESSAGE_DISPLAY_DELAY_MS));
        } else if (event.event_type === 'reactions' && event.reactions) {
            setMessages(prev => {
                const newMessages = [...prev];
                // Find message by round/turn match
                const searchStr = `msg-turn-${event.round_number}-${event.turn_index}-`;
                const targetIndex = newMessages.findIndex(m => m.id.startsWith(searchStr));

                if (targetIndex !== -1) {
                    const targetMessage = newMessages[targetIndex];
                    const newReactions: Reaction[] = event.reactions!.map((r: any) => ({
                        personaId: r.jurorId as PersonaType,
                        type: (r.reaction === '👍' || r.reaction.toLowerCase().includes('thumbs up')) ? 'thumbsUp' : 'thumbsDown'
                    }));

                    newMessages[targetIndex] = {
                        ...targetMessage,
                        reactions: newReactions
                    };
                }
                return newMessages;
            });
        } else if (event.event_type === 'verdict' && event.verdict) {
            const frontendVerdict = convertApiVerdictToFrontend(event.verdict);
            setVerdict(frontendVerdict);
            setProgress(100);
            options.onVerdict?.(frontendVerdict);
        } else if (event.event_type === 'complete') {
            setPhase('complete');
            setIsDebating(false);
            options.onComplete?.();
        } else if (event.event_type === 'error') {
            setPhase('error');
            setIsDebating(false);
            const err = new Error(event.content);
            setError(err);
            options.onError?.(err);
        }
    }, [options]);

    const processEventQueue = useCallback(async () => {
        if (isProcessingRef.current) return;
        isProcessingRef.current = true;

        while (eventQueueRef.current.length > 0) {
            if (stopRequestedRef.current) {
                eventQueueRef.current = [];
                break;
            }
            const event = eventQueueRef.current.shift();
            if (event) {
                await processEvent(event);
            }
        }

        isProcessingRef.current = false;
    }, [processEvent]);

    const startDebate = useCallback((claim: string, truth: string, model?: string) => {
        stopAudio();
        // Reset state
        setMessages([]);
        setPhase('intro');
        setCurrentSpeaker(null);
        setIsTyping(false);
        setVerdict(null);
        setError(null);
        setIsDebating(true);
        setProgress(0);
        eventQueueRef.current = [];
        stopRequestedRef.current = false;

        options.onStart?.();

        // Start streaming
        streamDebate(
            { claim, truth, model },
            (event) => {
                eventQueueRef.current.push(event);
                processEventQueue();
            },
            (err) => {
                setPhase('error');
                setIsDebating(false);
                setError(err);
                options.onError?.(err);
            },
            () => {
                // Streaming complete
            }
        );
    }, [options, processEventQueue, stopAudio]);

    const stopDebate = useCallback(() => {
        abortControllerRef.current?.abort();
        setIsDebating(false);
        stopAudio();
    }, [stopAudio]);

    const reset = useCallback(() => {
        stopDebate();
        setMessages([]);
        setPhase('idle');
        setCurrentSpeaker(null);
        setIsTyping(false);
        setVerdict(null);
        setError(null);
        setProgress(0);
        eventQueueRef.current = [];
    }, [stopDebate]);

    return {
        messages,
        phase,
        currentSpeaker,
        isTyping,
        verdict,
        error,
        isDebating,
        progress,
        startDebate,
        stopDebate,
        reset,
    };
}
