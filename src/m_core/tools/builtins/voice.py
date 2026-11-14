"""Voice AI tools for M AI Assistant - Speech-to-Text and Text-to-Speech."""
import asyncio
import base64
import os
import structlog
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from abc import ABC, abstractmethod

from m_core.common_schemas import ToolExecutionContext, ToolResult
from ..base import BaseTool

logger = structlog.get_logger(__name__)


# Check for optional voice dependencies
try:
    import faster_whisper
    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    FASTER_WHISPER_AVAILABLE = False

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

try:
    import kokoro
    KOKORO_AVAILABLE = True
except ImportError:
    KOKORO_AVAILABLE = False

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

try:
    import pvporcupine
    PV_PORCUPINE_AVAILABLE = True
except ImportError:
    PV_PORCUPINE_AVAILABLE = False

try:
    import sounddevice as sd
    import soundfile as sf
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False

try:
    import pvrecorder
    PVRECORDER_AVAILABLE = True
except ImportError:
    PVRECORDER_AVAILABLE = False


class VoiceProvider(ABC):
    """Abstract base class for voice providers."""
    
    @abstractmethod
    async def transcribe(self, audio_path: str) -> str:
        """Transcribe audio file to text."""
        pass
    
    @abstractmethod
    async def synthesize(self, text: str, output_path: str) -> bool:
        """Synthesize text to audio file."""
        pass


class WhisperSTTProvider:
    """Speech-to-Text using faster-whisper."""
    
    def __init__(self, model_size: str = "base", device: str = "auto", compute_type: str = "auto"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._model_lock = asyncio.Lock()
    
    async def _get_model(self):
        if self._model is None:
            async with self._model_lock:
                if self._model is None:
                    if not FASTER_WHISPER_AVAILABLE:
                        raise RuntimeError("faster-whisper not available")
                    
                    # Determine device
                    if self.device == "auto":
                        device = "cuda" if torch.cuda.is_available() else "cpu"
                    else:
                        device = self.device
                    
                    # Determine compute type
                    if self.compute_type == "auto":
                        compute_type = "float16" if device == "cuda" else "int8"
                    else:
                        compute_type = self.compute_type
                    
                    logger.info(f"Loading Whisper model: {self.model_size} on {device} with {compute_type}")
                    
                    # Load model in executor to avoid blocking
                    loop = asyncio.get_event_loop()
                    self._model = await loop.run_in_executor(
                        None,
                        lambda: faster_whisper.WhisperModel(
                            self.model_size,
                            device=device,
                            compute_type=compute_type
                        )
                    )
        return self._model
    
    async def transcribe(self, audio_path: str) -> str:
        """Transcribe audio file to text."""
        if not FASTER_WHISPER_AVAILABLE:
            raise RuntimeError("faster-whisper not available")
        
        model = await self._get_model()
        
        loop = asyncio.get_event_loop()
        segments, info = await loop.run_in_executor(
            None,
            lambda: model.transcribe(audio_path, beam_size=5, language="en")
        )
        
        # Collect all segments
        text_segments = []
        for segment in segments:
            text_segments.append(segment.text.strip())
        
        return " ".join(text_segments).strip()


class KokoroTTSProvider:
    """Text-to-Speech using Kokoro TTS."""
    
    def __init__(self, voice: str = "af_sky", speed: float = 1.0):
        self.voice = voice
        self.speed = speed
        self._model = None
        self._model_lock = asyncio.Lock()
    
    async def _get_model(self):
        if self._model is None:
            async with self._model_lock:
                if self._model is None:
                    if not KOKORO_AVAILABLE:
                        raise RuntimeError("Kokoro TTS not available")
                    
                    logger.info(f"Loading Kokoro TTS model with voice: {self.voice}")
                    
                    # Load model in executor
                    loop = asyncio.get_event_loop()
                    self._model = await loop.run_in_executor(
                        None,
                        lambda: kokoro.KPipeline(
                            lang_code='a',  # American English
                            repo_id='hexgrad/Kokoro-82M'
                        )
                    )
        return self._model
    
    async def synthesize(self, text: str, output_path: str, speed: float = None) -> bool:
        """Synthesize text to speech and save to file."""
        if not KOKORO_AVAILABLE:
            raise RuntimeError("Kokoro TTS not available")
        
        model = await self._get_model()
        speed = speed or self.speed
        
        try:
            # Generate audio
            loop = asyncio.get_event_loop()
            generator = await loop.run_in_executor(
                None,
                lambda: model(text, voice=self.voice, speed=speed)
            )
            
            # Collect audio chunks
            audio_chunks = []
            for i, (gs, ps, audio) in enumerate(generator):
                audio_chunks.append(audio)
            
            if not audio_chunks:
                raise RuntimeError("No audio generated")
            
            # Combine chunks
            import numpy as np
            import soundfile as sf
            combined_audio = np.concatenate(audio_chunks)
            
            # Save to file
            await asyncio.to_thread(sf.write, output_path, combined_audio, 24000)
            
            return True
            
        except Exception as e:
            logger.error(f"Kokoro TTS synthesis failed: {e}")
            raise


class EdgeTTSProvider:
    """Text-to-Speech using Microsoft Edge TTS (cloud-based, no local model needed)."""
    
    def __init__(self, voice: str = "en-US-AriaNeural", rate: str = "+0%", volume: str = "+0%"):
        self.voice = voice
        self.rate = rate
        self.volume = volume
    
    async def synthesize(self, text: str, output_path: str, rate: str = None, volume: str = None, voice: str = None) -> bool:
        """Synthesize text to speech and save to file using Edge TTS."""
        if not EDGE_TTS_AVAILABLE:
            raise RuntimeError("Edge TTS not available")
        
        voice = voice or self.voice
        rate = rate or self.rate
        volume = volume or self.volume
        
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
            await communicate.save(output_path)
            return True
        except Exception as e:
            logger.error(f"Edge TTS synthesis failed: {e}")
            raise
    
    @staticmethod
    async def list_voices() -> list:
        """List available Edge TTS voices."""
        if not EDGE_TTS_AVAILABLE:
            raise RuntimeError("Edge TTS not available")
        
        import edge_tts
        voices = await edge_tts.list_voices()
        return voices


class VoiceRecorder:
    """Audio recorder using sounddevice or pvrecorder."""
    
    def __init__(self, sample_rate: int = 16000, channels: int = 1, device: int = None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self._recording = False
        self._frames = []
        self._stream = None
    
    async def start_recording(self) -> bool:
        """Start recording audio."""
        if not SOUNDDEVICE_AVAILABLE and not PVRECORDER_AVAILABLE:
            raise RuntimeError("No audio recording backend available")
        
        if self._recording:
            return False
        
        self._frames = []
        self._recording = True
        
        try:
            if SOUNDDEVICE_AVAILABLE:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    device=self.device,
                    callback=self._audio_callback,
                    blocksize=1024,
                )
                self._stream.start()
            elif PVRECORDER_AVAILABLE:
                self._recorder = pvrecorder.PvRecorder(
                    frame_length=512,
                    device_index=self.device or -1,
                )
                self._recorder.start()
                # Start recording loop
                asyncio.create_task(self._record_loop())
            
            return True
        except Exception as e:
            self._recording = False
            logger.error(f"Failed to start recording: {e}")
            return False
    
    def _audio_callback(self, indata, frames, time, status):
        """Callback for sounddevice."""
        if self._recording:
            self._frames.append(indata.copy())
    
    async def _record_loop(self):
        """Recording loop for pvrecorder."""
        while self._recording and hasattr(self, '_recorder'):
            try:
                frame = self._recorder.read()
                # Convert to numpy array
                import numpy as np
                audio_data = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                self._frames.append(audio_data)
            except Exception:
                break
    
    async def stop_recording(self) -> Optional[str]:
        """Stop recording and save to temporary file."""
        if not self._recording:
            return None
        
        self._recording = False
        
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            elif hasattr(self, '_recorder') and self._recorder:
                self._recorder.stop()
                self._recorder = None
            
            if not self._frames:
                return None
            
            # Save to temporary file
            import tempfile
            import numpy as np
            import soundfile as sf
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                temp_path = tmp.name
            
            # Combine frames
            if SOUNDDEVICE_AVAILABLE:
                import numpy as np
                audio_data = np.concatenate(self._frames, axis=0)
            elif PVRECORDER_AVAILABLE:
                import numpy as np
                audio_data = np.concatenate(self._frames)
            else:
                return None
            
            # Save as WAV
            await asyncio.to_thread(
                sf.write,
                temp_path,
                audio_data,
                self.sample_rate,
                subtype='PCM_16'
            )
            
            return temp_path
            
        except Exception as e:
            logger.error(f"Failed to stop recording: {e}")
            return None


class WakeWordDetector:
    """Wake word detection using Porcupine."""
    
    def __init__(self, access_key: str, keywords: List[str] = None, sensitivities: List[float] = None):
        self.access_key = access_key
        self.keywords = keywords or ["porcupine", "hey google", "alexa", "hey siri", "computer"]
        self.sensitivities = sensitivities or [0.5] * len(self.keywords)
        self._porcupine = None
        self._recorder = None
        self._running = False
        self._callback = None
    
    async def initialize(self) -> bool:
        """Initialize Porcupine."""
        if not PV_PORCUPINE_AVAILABLE:
            logger.warning("Porcupine not available, wake word detection disabled")
            return False
        
        if not PVRECORDER_AVAILABLE:
            logger.warning("pvrecorder not available, wake word detection disabled")
            return False
        
        try:
            import pvporcupine
            
            self._porcupine = pvporcupine.create(
                access_key=self.access_key,
                keywords=self.keywords,
                sensitivities=self.sensitivities,
            )
            logger.info(f"Wake word detector initialized with keywords: {self.keywords}")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Porcupine: {e}")
            return False
    
    def set_callback(self, callback):
        """Set callback for wake word detection."""
        self._callback = callback
    
    async def start_listening(self) -> bool:
        """Start listening for wake words."""
        if not self._porcupine or not PVRECORDER_AVAILABLE:
            return False
        
        if self._running:
            return True
        
        try:
            import pvrecorder
            self._recorder = pvrecorder.PvRecorder(
                frame_length=self._porcupine.frame_length,
                device_index=-1,
            )
            self._recorder.start()
            self._running = True
            
            # Start listening loop
            asyncio.create_task(self._listen_loop())
            logger.info("Wake word detection started")
            return True
        except Exception as e:
            logger.error(f"Failed to start wake word detection: {e}")
            return False
    
    async def _listen_loop(self):
        """Listen for wake words."""
        while self._running and self._recorder:
            try:
                frame = self._recorder.read()
                keyword_index = self._porcupine.process(frame)
                
                if keyword_index >= 0:
                    keyword = self.keywords[keyword_index]
                    logger.info(f"Wake word detected: {keyword}")
                    if self._callback:
                        await self._callback(keyword)
            except Exception as e:
                logger.error(f"Wake word detection error: {e}")
                await asyncio.sleep(0.1)
    
    async def stop_listening(self) -> None:
        """Stop listening for wake words."""
        self._running = False
        if self._recorder:
            try:
                self._recorder.stop()
                self._recorder.delete()
            except:
                pass
            self._recorder = None
    
    def cleanup(self):
        """Clean up resources."""
        if self._porcupine:
            self._porcupine.delete()
            self._porcupine = None


# Voice Provider Manager
class VoiceManager:
    """Manages voice providers and recording."""
    
    def __init__(self):
        self.stt_provider: Optional[WhisperSTTProvider] = None
        self.tts_provider: Optional[object] = None  # Can be KokoroTTSProvider or EdgeTTSProvider
        self.tts_provider_type: str = "edge"  # "kokoro" or "edge"
        self.recorder: Optional[VoiceRecorder] = None
        self.wake_word_detector: Optional[WakeWordDetector] = None
        self._initialized = False
    
    async def initialize(
        self,
        whisper_model: str = "base",
        whisper_device: str = "auto",
        whisper_compute_type: str = "auto",
        tts_voice: str = "en-US-AriaNeural",
        tts_speed: float = 1.0,
        tts_provider: str = "edge",  # "kokoro" or "edge"
        porcupine_access_key: str = "",
        wake_keywords: List[str] = None,
    ) -> bool:
        """Initialize all voice components."""
        if self._initialized:
            return True
        
        success = True
        
        # Initialize STT
        try:
            self.stt_provider = WhisperSTTProvider(
                model_size=whisper_model,
                device=whisper_device,
                compute_type=whisper_compute_type,
            )
            logger.info("STT provider initialized")
        except Exception as e:
            logger.warning(f"STT provider initialization failed: {e}")
            success = False
        
        # Initialize TTS - prefer Edge TTS (works on Python 3.14)
        self.tts_provider_type = tts_provider
        if tts_provider == "edge" and EDGE_TTS_AVAILABLE:
            try:
                self.tts_provider = EdgeTTSProvider(voice=tts_voice)
                logger.info("Edge TTS provider initialized")
            except Exception as e:
                logger.warning(f"Edge TTS initialization failed: {e}")
                success = False
        elif tts_provider == "kokoro" and KOKORO_AVAILABLE:
            try:
                self.tts_provider = KokoroTTSProvider(
                    voice=tts_voice,
                    speed=tts_speed,
                )
                logger.info("Kokoro TTS provider initialized")
            except Exception as e:
                logger.warning(f"Kokoro TTS initialization failed: {e}")
                success = False
        elif EDGE_TTS_AVAILABLE:
            # Fallback to Edge TTS
            try:
                self.tts_provider = EdgeTTSProvider(voice=tts_voice)
                self.tts_provider_type = "edge"
                logger.info("Edge TTS provider initialized (fallback)")
            except Exception as e:
                logger.warning(f"Edge TTS fallback initialization failed: {e}")
                success = False
        else:
            logger.warning("No TTS provider available")
            success = False
        
        # Initialize recorder
        try:
            self.recorder = VoiceRecorder()
            logger.info("Voice recorder initialized")
        except Exception as e:
            logger.warning(f"Recorder initialization failed: {e}")
            success = False
        
        # Initialize wake word detector
        if porcupine_access_key:
            try:
                self.wake_word_detector = WakeWordDetector(
                    access_key=porcupine_access_key,
                    keywords=wake_keywords,
                )
                initialized = await self.wake_word_detector.initialize()
                if initialized:
                    logger.info("Wake word detector initialized")
                else:
                    logger.warning("Wake word detector failed to initialize")
                    success = False
            except Exception as e:
                logger.warning(f"Wake word detector initialization failed: {e}")
                success = False
        
        self._initialized = True
        return success
    
    def get_stt_provider(self) -> Optional[WhisperSTTProvider]:
        return self.stt_provider
    
    def get_tts_provider(self) -> Optional[object]:
        return self.tts_provider
    
    def get_tts_provider_type(self) -> str:
        return self.tts_provider_type
    
    def get_recorder(self) -> Optional[VoiceRecorder]:
        return self.recorder
    
    def get_wake_word_detector(self) -> Optional[WakeWordDetector]:
        return self.wake_word_detector
    
    async def cleanup(self):
        """Clean up all resources."""
        if self.wake_word_detector:
            await self.wake_word_detector.stop_listening()
            self.wake_word_detector.cleanup()
        if self.recorder and self.recorder._recording:
            await self.recorder.stop_recording()


# Global voice manager instance
voice_manager = VoiceManager()


# Voice Tools
class VoiceRecordTool(BaseTool):
    """Record audio from microphone."""
    
    manifest = {
        "name": "voice_record",
        "display_name": "Record Voice",
        "description": "Record audio from microphone for voice input.",
        "category": "voice",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "integer", "default": 10, "description": "Recording duration in seconds"},
                "sample_rate": {"type": "integer", "default": 16000, "description": "Sample rate"},
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "audio_file": {"type": "string"},
                "duration_seconds": {"type": "number"},
            },
        },
        "required_permissions": ["voice:mic"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Records audio from microphone.",
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["voice", "record", "microphone", "stt"],
    }
    
    async def _execute_impl(self, context) -> dict:
        recorder = voice_manager.get_recorder()
        if not recorder:
            return {"success": False, "error": "Voice recorder not initialized", "error_code": "NOT_INITIALIZED"}
        
        args = context.arguments
        duration = args.get("duration_seconds", 10)
        
        # Start recording
        success = await recorder.start_recording()
        if not success:
            return {"success": False, "error": "Failed to start recording", "error_code": "RECORDING_FAILED"}
        
        # Wait for duration
        await asyncio.sleep(duration)
        
        # Stop and save
        audio_file = await recorder.stop_recording()
        
        if not audio_file:
            return {"success": False, "error": "No audio recorded", "error_code": "NO_AUDIO"}
        
        return {
            "success": True,
            "result": {
                "audio_file": audio_file,
                "duration_seconds": duration,
            },
            "error": None,
        }


class VoiceTranscribeTool(BaseTool):
    """Transcribe audio file to text using STT."""
    
    manifest = {
        "name": "voice_transcribe",
        "display_name": "Transcribe Audio",
        "description": "Transcribe audio file to text using Whisper STT.",
        "category": "voice",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "audio_file": {"type": "string", "description": "Path to audio file"},
                "language": {"type": "string", "default": "en", "description": "Language code"},
            },
            "required": ["audio_file"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "language": {"type": "string"},
            },
        },
        "required_permissions": ["voice:stt"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Transcribes audio to text.",
        "requires_approval": False,
        "timeout_seconds": 60,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["voice", "transcribe", "stt", "whisper"],
    }
    
    async def _execute_impl(self, context) -> dict:
        stt_provider = voice_manager.get_stt_provider()
        if not stt_provider:
            return {"success": False, "error": "STT provider not initialized", "error_code": "NOT_INITIALIZED"}
        
        args = context.arguments
        audio_file = args.get("audio_file")
        
        if not audio_file:
            return {"success": False, "error": "audio_file is required", "error_code": "INVALID_INPUT"}
        
        audio_path = Path(audio_file)
        if not audio_path.exists():
            return {"success": False, "error": f"Audio file not found: {audio_file}", "error_code": "NOT_FOUND"}
        
        try:
            text = await stt_provider.transcribe(str(audio_path))
            return {
                "success": True,
                "result": {
                    "text": text,
                    "language": "en",
                },
                "error": None,
            }
        except Exception as e:
            return {"success": False, "error": f"Transcription failed: {str(e)}", "error_code": "TRANSCRIPTION_FAILED"}


class VoiceSpeakTool(BaseTool):
    """Synthesize speech from text using TTS."""
    
    manifest = {
        "name": "voice_speak",
        "display_name": "Speak Text",
        "description": "Convert text to speech using Edge TTS or Kokoro TTS.",
        "category": "voice",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to speak"},
                "voice": {"type": "string", "default": "en-US-AriaNeural", "description": "Voice to use (Edge TTS: en-US-AriaNeural, Kokoro: af_sky)"},
                "rate": {"type": "string", "default": "+0%", "description": "Speech rate for Edge TTS (e.g., +10%, -10%)"},
                "volume": {"type": "string", "default": "+0%", "description": "Volume for Edge TTS (e.g., +10%, -10%)"},
                "speed": {"type": "number", "default": 1.0, "description": "Speech speed for Kokoro TTS"},
                "output_file": {"type": "string", "description": "Output file path (optional)"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "audio_file": {"type": "string"},
                "duration_estimate": {"type": "number"},
            },
        },
        "required_permissions": ["voice:tts"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Converts text to speech.",
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["voice", "speak", "tts", "edge", "kokoro"],
    }
    
    async def _execute_impl(self, context) -> dict:
        tts_provider = voice_manager.get_tts_provider()
        tts_type = voice_manager.get_tts_provider_type()
        if not tts_provider:
            return {"success": False, "error": "TTS provider not initialized", "error_code": "NOT_INITIALIZED"}
        
        args = context.arguments
        text = args.get("text")
        voice = args.get("voice", "en-US-AriaNeural")
        rate = args.get("rate", "+0%")
        volume = args.get("volume", "+0%")
        speed = args.get("speed", 1.0)
        output_file = args.get("output_file")
        
        if not text:
            return {"success": False, "error": "text is required", "error_code": "INVALID_INPUT"}
        
        # Create output file if not specified
        if not output_file:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                output_file = tmp.name
        
        try:
            if tts_type == "edge":
                await tts_provider.synthesize(text, output_file, rate=rate, volume=volume, voice=voice)
            else:
                await tts_provider.synthesize(text, output_file, speed=speed)
            
            # Estimate duration (rough estimate)
            duration_estimate = len(text) / (15 * speed)  # ~15 chars per second at 1x speed
            
            return {
                "success": True,
                "result": {
                    "audio_file": output_file,
                    "duration_estimate": duration_estimate,
                },
                "error": None,
            }
        except Exception as e:
            return {"success": False, "error": f"TTS synthesis failed: {str(e)}", "error_code": "TTS_FAILED"}


class WakeWordListenTool(BaseTool):
    """Start/stop wake word detection."""
    
    manifest = {
        "name": "wake_word_listen",
        "display_name": "Wake Word Listen",
        "description": "Start or stop wake word detection.",
        "category": "voice",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "status"], "description": "Action to perform"},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "Wake word keywords"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "running": {"type": "boolean"},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required_permissions": ["voice:wake-word"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Starts/stops wake word detection.",
        "requires_approval": False,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["voice", "wake-word", "porcupine"],
    }
    
    async def _execute_impl(self, context) -> dict:
        detector = voice_manager.get_wake_word_detector()
        if not detector:
            return {"success": False, "error": "Wake word detector not initialized", "error_code": "NOT_INITIALIZED"}
        
        args = context.arguments
        action = args.get("action", "status")
        
        if action == "start":
            keywords = args.get("keywords")
            if keywords:
                # Update keywords if provided
                detector.keywords = keywords
            success = await detector.start_listening()
            return {
                "success": success,
                "result": {"running": True},
                "error": None,
            }
        elif action == "stop":
            await detector.stop_listening()
            return {
                "success": True,
                "result": {"running": False},
                "error": None,
            }
        else:
            return {
                "success": True,
                "result": {
                    "running": detector._running,
                    "keywords": detector.keywords,
                },
                "error": None,
            }


# Voice Tool Registration
VOICE_TOOLS = [
    "VoiceRecordTool",
    "VoiceTranscribeTool",
    "VoiceSpeakTool",
    "WakeWordListenTool",
]

__all__ = [
    "VoiceManager",
    "WhisperSTTProvider",
    "KokoroTTSProvider",
    "EdgeTTSProvider",
    "VoiceRecorder",
    "WakeWordDetector",
    "voice_manager",
    "VoiceRecordTool",
    "VoiceTranscribeTool",
    "VoiceSpeakTool",
    "WakeWordListenTool",
    "VOICE_TOOLS",
    "FASTER_WHISPER_AVAILABLE",
    "KOKORO_AVAILABLE",
    "EDGE_TTS_AVAILABLE",
    "PV_PORCUPINE_AVAILABLE",
    "SOUNDDEVICE_AVAILABLE",
    "PVRECORDER_AVAILABLE",
    "TORCH_AVAILABLE",
]