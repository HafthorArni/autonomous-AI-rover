import signal
import socket
import base64
import time
import threading
import queue
import io
import sys
import random
import paramiko # For SSH
from dotenv import load_dotenv

import cv2
import openai
from elevenlabs.client import ElevenLabs
from elevenlabs import play as play_audio
from pydub import AudioSegment

# === CONFIGURATION === ------------------------------------------------------
# Load the environment variables from the .env file
load_dotenv()
try:
    # Explicitly pull the keys from the environment variables
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    client_11 = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))
except Exception as e:
    print(f"[Config Error] Failed to initialise API clients: {e}", file=sys.stderr)
    sys.exit(1)

# --- Raspberry Pi Configuration ---
RPI_IP = os.environ.get("RPI_IP")
RPI_PORT = 22 # Standard SSH port
RPI_USER = os.environ.get("RPI_USER")
RPI_PASSWORD = os.environ.get("RPI_PASSWORD")
RPI_KEY_FILENAME = None # Set to None since using password

RPI_MOTOR_SCRIPT_NAME = "body.py" # Name of the script on the RPi
RPI_MOTOR_SCRIPT_LOG = f"/home/{RPI_USER}/body.log" # Log file path on RPi

# --- Motor Control Configuration ---
MOTOR_IP = RPI_IP # RPi IP address (where body.py runs)
MOTOR_CONTROL_PORT = 5005
FORWARD_DURATION = 3.0   # seconds
TURN_DURATION = 1.5      # seconds

FORWARD_SPEED_LEFT = 60   # Speed for left motor when moving forward (0-100)
FORWARD_SPEED_RIGHT = 60  # Speed for right motor when moving forward (0-100)
BACKWARD_SPEED_LEFT = 60  # Speed for left motor when moving backward (0-100)
BACKWARD_SPEED_RIGHT = 60 # Speed for right motor when moving backward (0-100)

TURN_SPEED = 40          # Default speed for left/right turns (0-100)

# --- Narration & AI Configuration ---
#VOICE_ID = "a8p00hpqmTpR1cLnk76X" # aussie
#VOICE_ID = "vJnOqGC8uWYZxHYiEVfu" # paranoid android
#VOICE_ID = "wDsJlOXPqcvIUKdLXjDs" # jarvis
VOICE_ID = "xYWUvKNK6zWCgsdAK7Wi" # Maverick

VOICE_SPEED = 1.2 # Note: Speed adjustment might not work with all ElevenLabs models/APIs
NARRATION_LEAD_TIME_S = 3.0 # How much of end of narration can be cut off by next cycle (original interpretation)
MAX_NARRATION_SKIP = 2 # Max number of cycles to skip narration (0 means narrate every time)
MODEL_ANALYSIS = "gpt-4o-mini"
MODEL_DESCRIPTION = "gpt-4o-mini"

# --- Video Configuration ---
ENABLE_VIDEO_WINDOW = True
PI_VIDEO_STREAM_URL = f"tcp://{MOTOR_IP}:8888"
VIDEO_WIDTH = 740 # Match the libcamera-vid command on RPi
VIDEO_HEIGHT = 600 # Match the libcamera-vid command on RPi
VIDEO_CODEC = "h264" # Match the libcamera-vid command on RPi

IMAGE_LIMIT = 0          # 0 = unlimited analysis cycles
JPEG_WH = (320, 240)     # down‑scale frame before sending to OpenAI
JPEG_QUALITY = 50        # JPEG compression quality (0-100)

# --- Narration Memory ---
narration_history: list[str] = []

# === SHARED RESOURCES === ---------------------------------------------------
analysis_result_queue = queue.Queue(maxsize=1)
narration_info_queue = queue.Queue(maxsize=1)

shutdown_flag = threading.Event() # Global flag to signal shutdown

frame_lock = threading.Lock() # Protects access to latest_frame
latest_frame = None
stream_active = threading.Event() # Signals if video stream is believed active

# Pre‑open UDP socket (reused for sending motor commands)
try:
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
except socket.error as e:
     print(f"[Error] Failed to create UDP socket: {e}", file=sys.stderr)
     sys.exit(1)

# === SSH Client === ---------------------------------------------------------
ssh_client = None # Global SSH client instance

# ----------------------------------------------------------------------------
#                         SIGNAL HANDLING (reliable Ctrl‑C)
# ----------------------------------------------------------------------------

def handle_sigint(signum, frame):
    """Ensure immediate, clean shutdown on Ctrl‑C."""
    if shutdown_flag.is_set():
        print("[Signal] Second Ctrl‑C – Forcing exit now.")
        try: send_drive_command("stop", 0, 0) # Attempt last stop (pass dummy speeds)
        except: pass # nosec
        sys.exit(1)

    print("\n[Signal] Ctrl‑C received. Initiating shutdown sequence...")
    shutdown_flag.set()
    try: send_drive_command("stop", 0, 0) # Pass dummy speeds for the new signature
    except Exception as e: print(f"[Signal] Error sending final stop command: {e}")

signal.signal(signal.SIGINT, handle_sigint)

# ----------------------------------------------------------------------------
#                              REMOTE SCRIPT STARTUP / SHUTDOWN
# ----------------------------------------------------------------------------

# Functions start_remote_scripts() and stop_remote_scripts() remain the same
# as in the previous correct answer (including the 'global ssh_client' fix).
# Make sure you have the versions that log body.py output to RPI_MOTOR_SCRIPT_LOG
# and handle killing previous instances.

def start_remote_scripts():
    """Connects to RPi via SSH and starts video stream and motor control."""
    global ssh_client # We need to modify the global variable
    print("[SSH] Attempting to connect to RPi...")
    try:
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy()) # Auto-accept host key

        auth_args = {'hostname': RPI_IP, 'port': RPI_PORT, 'username': RPI_USER}
        if RPI_KEY_FILENAME:
            auth_args['key_filename'] = RPI_KEY_FILENAME
            print(f"[SSH] Using key file: {RPI_KEY_FILENAME}")
        elif RPI_PASSWORD:
            auth_args['password'] = RPI_PASSWORD
            print("[SSH] Using password authentication.")
        else:
             print("[SSH] Error: No SSH key file or password provided in config.", file=sys.stderr)
             return False

        ssh_client.connect(**auth_args, timeout=10) # 10 second connection timeout
        print("[SSH] Connected successfully.")

        # --- Command Definitions ---
        # Kill existing instances first (optional, but safer)
        kill_video_cmd = f"pkill -f 'libcamera-vid.*{VIDEO_CODEC}' || true" # Ignore error if not running
        kill_motor_cmd = f"pkill -f 'python {RPI_MOTOR_SCRIPT_NAME}' || true"

        # Start commands using nohup to detach from SSH session
        video_cmd = (
            f"nohup libcamera-vid -t 0 --inline --listen -o tcp://0.0.0.0:8888 "
            f"--width {VIDEO_WIDTH} --height {VIDEO_HEIGHT} --codec {VIDEO_CODEC} "
            f"> /dev/null 2>&1 &" # Redirect output to /dev/null
        )
        motor_cmd = (
             f"nohup python {RPI_MOTOR_SCRIPT_NAME} "
             f"> {RPI_MOTOR_SCRIPT_LOG} 2>&1 &" # Redirect output to log file
        )
        print(f"[SSH] Motor control log will be at: {RPI_MOTOR_SCRIPT_LOG}")

        # --- Execute Commands ---
        print("[SSH] Stopping any previous instances...")
        stdin, stdout, stderr = ssh_client.exec_command(kill_video_cmd)
        stdout.channel.recv_exit_status() # Wait for command to finish
        stdin, stdout, stderr = ssh_client.exec_command(kill_motor_cmd)
        stdout.channel.recv_exit_status()
        time.sleep(0.5) # Brief pause

        print("[SSH] Starting remote video stream...")
        stdin, stdout, stderr = ssh_client.exec_command(video_cmd)
        exit_status = stdout.channel.recv_exit_status() # Check immediate exit status
        if exit_status != 0: print(f"[SSH] Warning: Video command exited immediately with status {exit_status}. Stderr: {stderr.read().decode()}")
        time.sleep(1) # Small delay to allow process to potentially start/fail

        print(f"[SSH] Starting remote motor control ({RPI_MOTOR_SCRIPT_NAME})...")
        stdin, stdout, stderr = ssh_client.exec_command(motor_cmd)
        exit_status = stdout.channel.recv_exit_status() # Check immediate exit status
        if exit_status != 0: print(f"[SSH] Warning: Motor command exited immediately with status {exit_status}. Stderr: {stderr.read().decode()}")
        time.sleep(1) # Small delay

        print("[SSH] Remote scripts launched (check RPi if issues occur).")
        # Keep SSH client open for stopping scripts later
        return True

    except paramiko.AuthenticationException:
        print(f"[SSH] Authentication failed for user '{RPI_USER}'. Check credentials.", file=sys.stderr)
        ssh_client = None # Ensure client is None on failure
        return False
    except Exception as e:
        print(f"[SSH] Error connecting or executing commands: {e}", file=sys.stderr)
        if ssh_client:
            try: ssh_client.close()
            except: pass # nosec
        ssh_client = None # Ensure client is None on failure
        return False

def stop_remote_scripts():
    """Attempts to stop the remote scripts gracefully via SSH."""
    global ssh_client # Need access to the global client instance
    if not ssh_client:
        print("[SSH] No active SSH client to stop remote scripts.")
        return

    print("[SSH] Attempting to stop remote scripts...")
    try:
        # More specific kill commands if possible
        kill_video_cmd = f"pkill -f 'libcamera-vid.*{VIDEO_CODEC}'"
        kill_motor_cmd = f"pkill -f 'python {RPI_MOTOR_SCRIPT_NAME}'"

        print(f"[SSH] Sending command: {kill_video_cmd}")
        stdin, stdout, stderr = ssh_client.exec_command(kill_video_cmd)
        exit_status_video = stdout.channel.recv_exit_status() # Wait
        err_output_vid = stderr.read().decode().strip()
        if err_output_vid and 'no process found' not in err_output_vid.lower(): print(f"[SSH] Kill Video Output/Error: {err_output_vid}")
        time.sleep(0.2)

        print(f"[SSH] Sending command: {kill_motor_cmd}")
        stdin, stdout, stderr = ssh_client.exec_command(kill_motor_cmd)
        exit_status_control = stdout.channel.recv_exit_status() # Wait
        err_output_ctrl = stderr.read().decode().strip()
        if err_output_ctrl and 'no process found' not in err_output_ctrl.lower(): print(f"[SSH] Kill Motor Output/Error: {err_output_ctrl}")

    except Exception as e:
        print(f"[SSH] Error sending kill commands: {e}", file=sys.stderr)
    finally:
        if ssh_client:
            try:
                ssh_client.close()
                print("[SSH] Connection closed.")
            except Exception as e:
                 print(f"[SSH] Error closing connection: {e}", file=sys.stderr)
        ssh_client = None # Mark as closed/None

# ----------------------------------------------------------------------------
#                               WORKERS
# ----------------------------------------------------------------------------

# _encode_small_jpeg, analysis_worker, description_worker, get_description_text,
# generate_and_play_audio functions remain the same as in the previous correct
# answer (including the narration event fix in generate_and_play_audio).

def _encode_small_jpeg(frame):
    """Resizes, encodes frame to JPEG, and returns base64 string."""
    try:
        small = cv2.resize(frame, JPEG_WH, interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            print("[Error] cv2.imencode failed.", file=sys.stderr)
            return None
        return base64.b64encode(enc).decode('utf-8')
    except Exception as e:
        print(f"[Error] _encode_small_jpeg failed: {e}", file=sys.stderr)
        return None


def analysis_worker(frame_copy):
    """Sends frame to OpenAI for navigation command analysis."""
    if frame_copy is None or shutdown_flag.is_set():
        try: analysis_result_queue.put_nowait("stop")
        except queue.Full: pass
        return

    try:
        base64_img = _encode_small_jpeg(frame_copy)
        if not base64_img:
             raise ValueError("Failed to encode frame for analysis.")

        resp = client.chat.completions.create(
            model=MODEL_ANALYSIS,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "You are a small robot navigating an apartment using camera images. "
                        "Decide the next move. Your options are ONLY: 'forward', 'left', 'right', or 'backward'. "
                        "Reply 'left' or 'right' if an obstacle is very close (less than 50cm). "
                        "Reply 'backward' ONLY if the image is completely blocked (e.g., all white/black). "
                        "Otherwise, reply 'forward'. Prioritize 'forward' if clear. "
                        "Reply ONLY with the single chosen word."
                    )},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}", "detail": "low"}}
                ]}],
            max_tokens=5 # Only need one word
        )
        cmd = resp.choices[0].message.content.strip().lower()

        # Validate command
        valid_cmds = ("forward", "left", "right", "backward")
        if cmd not in valid_cmds:
             print(f"[Analysis] Warning: Unexpected command '{cmd}' from API, defaulting to 'forward'.")
             cmd = "forward"
        analysis_result_queue.put(cmd)

    except Exception as e:
        print(f"[Error] analysis_worker failed: {e}", file=sys.stderr)
        try: analysis_result_queue.put_nowait("forward") # Default command on error
        except queue.Full: pass


def description_worker(frame_copy, playback_evt_main):
    """Generates description, streams audio, calculates duration, puts info in queue."""
    # Renamed playback_evt to playback_evt_main for clarity
    if frame_copy is None or shutdown_flag.is_set():
        try:
            # Put dummy info if not narrating or shutting down
            narration_info_queue.put_nowait({'duration': 0.0, 'event': playback_evt_main})
            playback_evt_main.set() # Ensure event is set
        except queue.Full: pass
        return

    desc_text = None
    audio_duration = 0.0
    t_start = time.perf_counter()
    print(f"[Timing] Desc Start: {t_start:.3f}s")

    try:
        # --- Get Description ---
        desc_text = get_description_text(frame_copy)
        t_openai_done = time.perf_counter()
        print(f"[Timing] OpenAI Done: {t_openai_done:.3f}s (+{t_openai_done - t_start:.3f}s)")

        if not desc_text or shutdown_flag.is_set():
            print("[DescWorker] No description generated or shutting down.")
            narration_info_queue.put({'duration': 0.0, 'event': playback_evt_main})
            playback_evt_main.set() # Ensure event is set
            return

        narration_history.append(desc_text) # Add to history
        print(f"[DescWorker] Description: '{desc_text}'")

        # --- Stream Audio & Calculate Duration ---
        t_tts_start = time.perf_counter()
        print(f"[Timing] TTS Start: {t_tts_start:.3f}s (+{t_tts_start - t_openai_done:.3f}s)")

        audio_stream = None
        try:
            audio_stream = client_11.text_to_speech.convert_as_stream(
                text=desc_text,
                voice_id=VOICE_ID,
                model_id="eleven_turbo_v2_5" # Or other suitable model
            )
        except Exception as e:
             print(f"[DescWorker] ElevenLabs API Error: {e}", file=sys.stderr)
             narration_info_queue.put({'duration': 0.0, 'event': playback_evt_main})
             playback_evt_main.set()
             return # Stop processing if TTS fails


        audio_buffer = bytearray()
        # These events are internal to description_worker and its threads
        playback_started_internal = threading.Event()
        stream_complete_internal = threading.Event()

        # Thread to play audio & signal main loop event (playback_evt_main)
        play_thread = threading.Thread(
            target=generate_and_play_audio,
            args=(audio_stream, audio_buffer, playback_evt_main, playback_started_internal, stream_complete_internal, t_tts_start),
            daemon=True
        )
        play_thread.start()

        # Thread to calculate duration (waits for stream_complete_internal)
        # This now only calculates duration and puts the final result in the queue
        duration_thread = threading.Thread(
             target=calculate_audio_duration,
             args=(audio_buffer, playback_evt_main, stream_complete_internal, t_tts_start),
             daemon=True
        )
        duration_thread.start()

    except Exception as e:
        print(f"[DescWorker Error] Unexpected error in description worker: {e}", file=sys.stderr)
        try:
            narration_info_queue.put_nowait({'duration': 0.0, 'event': playback_evt_main})
        except queue.Full: pass
        playback_evt_main.set() # Ensure event is set on error


def get_description_text(frame):
    """Sends frame to OpenAI for a one-sentence description."""
    if shutdown_flag.is_set(): return None

    base64_img = _encode_small_jpeg(frame)
    if not base64_img: return None # Encoding failed

    # Build context from recent history
    context = ""
    if narration_history:
        context = " Previously you said: " + " | ".join(narration_history[-5:]) # Last 3 descriptions

    prompt = (
        "You are a small, slightly sarcastic and a little rude robot exploring an apartment. "
        f"Describe what you see in one short sentence (max 15 words). Only talk in one short sentence (max ~15 words). Avoid being repetitive {context}"
        " If this is your first observation (no context given), express brief surprise or curiosity."
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL_DESCRIPTION,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}", "detail": "low"}}
                ]
            }],
            max_tokens=60 # Ample space for a short sentence
        )
        description = resp.choices[0].message.content.strip()

        if not description or len(description) > 120: # Allow slightly longer description
             print(f"[DescWorker] Warning: Invalid description length: {len(description)}")
             return "I see... something." # Fallback
        description = description.strip('\'"') # Remove potential quotes
        return description
    except Exception as e:
        print(f"[DescWorker] OpenAI description API call failed: {e}", file=sys.stderr)
        return None


def generate_and_play_audio(audio_stream, buffer, playback_evt_main, playback_started_internal, stream_complete_internal, t_tts_start):
    """Streams audio, plays it, signals events, handles shutdown."""
    first_chunk_received = False
    try:
        def chunk_stream_wrapper():
            nonlocal first_chunk_received
            for chunk in audio_stream:
                if shutdown_flag.is_set(): break
                if isinstance(chunk, (bytes, bytearray)) and chunk:
                    buffer.extend(chunk) # Add chunk to buffer
                    if not playback_started_internal.is_set():
                        t_play = time.perf_counter()
                        print(f"[Timing] Playback Start: {t_play:.3f}s (+{t_play - t_tts_start:.3f}s)")
                        playback_started_internal.set() # Signal internal start
                        playback_evt_main.set()     # <<< Signal MAIN loop event >>>
                        first_chunk_received = True
                    yield chunk # Yield chunk to the player
                elif not chunk: time.sleep(0.01)

            t_stream_done = time.perf_counter()
            if not shutdown_flag.is_set():
                 print(f"[Timing] Stream Done: {t_stream_done:.3f}s (+{t_stream_done - t_tts_start:.3f}s)")
            stream_complete_internal.set() # Signal internal completion

        play_audio(chunk_stream_wrapper()) # Start playing

        if not first_chunk_received: # Ensure events are set if no data arrived
             playback_started_internal.set()
             stream_complete_internal.set()
             playback_evt_main.set()

    except Exception as e:
         print(f"[Audio] Playback error: {e}", file=sys.stderr)
         playback_started_internal.set() # Ensure events are set on error
         stream_complete_internal.set()
         playback_evt_main.set()


def calculate_audio_duration(buffer, playback_evt_main, stream_complete_internal, t_tts_start):
    """ Calculates duration AFTER full audio is buffered and puts final result in queue. """
    narration_info_to_put = {'duration': 0.0, 'event': playback_evt_main} # Default info

    try:
        # Wait for the streaming/playing thread to signal completion
        if not stream_complete_internal.wait(timeout=25.0): # Increased timeout slightly
            if not shutdown_flag.is_set():
                print("[DescWorker] Warning: Timeout waiting for audio stream to complete for duration calculation.")
            # Keep duration 0.0
            return # Exit early, will put default info in finally block

        if shutdown_flag.is_set(): # Check if shutdown happened during wait
            # Keep duration 0.0
            return # Exit early, will put default info in finally block

        # Proceed with calculation if stream completed and not shutting down
        if buffer:
            try:
                # Use BytesIO for in-memory data
                audio_data = io.BytesIO(buffer)
                audio = AudioSegment.from_file(audio_data, format="mp3")
                # Ensure duration is positive, handle potential calculation quirks
                calc_duration = max(0.0, audio.duration_seconds)
                narration_info_to_put['duration'] = calc_duration # Update duration in dict
                t_dur_ready = time.perf_counter()
                print(f"[Timing] Duration Ready: {t_dur_ready:.3f}s (+{t_dur_ready - t_tts_start:.3f}s) — Duration: {calc_duration:.2f}s")
            except Exception as e:
                # Log specific error during pydub processing
                print(f"[DescWorker] Pydub duration calculation failed: {e}", file=sys.stderr)
                print(f"[DescWorker] Buffer size was: {len(buffer)} bytes")
                # Keep duration 0.0
        else:
            print("[DescWorker] Audio buffer empty after stream completion, duration is 0.")
            # Keep duration 0.0

    except Exception as e:
        print(f"[DescWorker] Unexpected error in calculate_audio_duration: {e}", file=sys.stderr)
        # Ensure duration is 0.0 on unexpected errors

    finally:
        # Put the final info (calculated duration or 0.0 on error/timeout) into the queue
        if not shutdown_flag.is_set(): # Don't bother putting if shutting down
            try:
                # Ensure the queue is clear before putting the definitive result for this cycle
                try:
                    _ = narration_info_queue.get_nowait()
                    print("[DescWorker] Cleared potentially stale item from narration_info_queue.")
                except queue.Empty:
                    pass # Queue was already empty, proceed

                narration_info_queue.put(narration_info_to_put, timeout=0.5) # Use put with timeout
            except queue.Full:
                print("[DescWorker] Narration info queue full! Main loop likely blocked.", file=sys.stderr)
            except Exception as e:
                 print(f"[DescWorker] Error putting final narration info: {e}", file=sys.stderr)
        else:
            print("[DescWorker] Shutdown occurred, not putting final narration info.")

# ----------------------------------------------------------------------------
#                              UTILITIES
# ----------------------------------------------------------------------------

def send_drive_command(cmd: str, speed_left: int, speed_right: int):
    """
    Sends command via UDP.
    For 'forward'/'backward', sends 'command:speed_left:speed_right'.
    For 'left'/'right', sends 'command:speed' (using speed_left as the turn speed).
    For 'stop', sends 'stop:0'.
    """
    global udp_sock
    if shutdown_flag.is_set() and cmd != "stop": return # Don't send if shutting down

    try:
        cmd_lower = cmd.lower().strip()
        message = ""

        if cmd_lower in ("forward", "backward"):
            # Use new format for differential speed
            message = f"{cmd_lower}:{int(speed_left)}:{int(speed_right)}"
        elif cmd_lower in ("left", "right"):
            # Use old format for turns, sending the single TURN_SPEED via speed_left
            # RPi body.py will use this single speed value for both motors but in opposite directions
            message = f"{cmd_lower}:{int(speed_left)}"
        elif cmd_lower == "stop":
            # Stop command still simple, speed value is ignored by RPi but send 0
            message = "stop:0"
        else:
            print(f"[Error] Unknown command '{cmd}' in send_drive_command.", file=sys.stderr)
            return # Don't send unknown commands

        # print(f"[UDP Send] Sending: '{message}'") # Optional debug print
        udp_sock.sendto(message.encode('utf-8'), (MOTOR_IP, MOTOR_CONTROL_PORT))

    except NameError:
        print("[Error] UDP socket not initialized.", file=sys.stderr)
    except socket.error as e:
        print(f"[Error] UDP Send failed for '{message}': {e}", file=sys.stderr)
    except Exception as e:
        print(f"[Error] UDP Send unexpected error for '{message}': {e}", file=sys.stderr)

# frame_reader_thread and video_display_worker remain the same as previous answer
def frame_reader_thread(cap, url):
    """ Reads frames from video stream, handles reconnections, signals activity. """
    global latest_frame
    reconnect_delay = 2.0
    consecutive_read_failures = 0
    max_read_failures = 5 # How many read errors before attempting reconnect

    while not shutdown_flag.is_set():
        if not cap.isOpened():
            if stream_active.is_set(): # Only print disconnect message once
                print(f"[Video] Stream disconnected. Reconnecting in {reconnect_delay:.1f}s...")
                stream_active.clear()

            if shutdown_flag.wait(reconnect_delay): break # Exit if shutdown during wait

            print(f"[Video] Attempting to reconnect to {url}...")
            cap.release() # Ensure old object is released
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG) # Re-initialize
            reconnect_delay = min(reconnect_delay * 1.5, 30.0) # Increase delay backoff
            consecutive_read_failures = 0 # Reset failures on reconnect attempt
            continue

        # --- If connected or reconnected successfully ---
        reconnect_delay = 2.0 # Reset reconnect delay on success
        ok, frame = cap.read()

        if not ok:
            consecutive_read_failures += 1
            if consecutive_read_failures >= max_read_failures:
                 if stream_active.is_set():
                     print("[Video] Failed to read frame multiple times. Releasing capture, will attempt reconnect.")
                     stream_active.clear()
                 cap.release() # Release capture to force reconnect attempt
                 time.sleep(0.5) # Brief pause before trying to reopen in next loop iteration
            else:
                time.sleep(0.1) # Small delay before trying next read
            continue

        # --- Frame read successfully ---
        consecutive_read_failures = 0 # Reset counter on successful read
        with frame_lock:
            latest_frame = frame
        if not stream_active.is_set():
             print("[Video] Stream is active.")
             stream_active.set() # Signal stream is now active

        time.sleep(0.01) # Small delay to yield control

    # --- Cleanup on exit ---
    if cap.isOpened():
        print("[Video] Releasing video capture...")
        cap.release()
    stream_active.clear() # Ensure state is inactive
    print("[Video] Frame reader thread finished.")


def video_display_worker():
    """ Displays the video stream in a window if enabled. Handles window closing."""
    if not ENABLE_VIDEO_WINDOW:
        print("[Video Display] Disabled.")
        return

    win_name = "Robot Video Stream"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win_name, JPEG_WH[0]*2, JPEG_WH[1]*2) # Start with a reasonable size

    while not shutdown_flag.is_set():
        frame_to_show = None
        display_message = None

        try:
            if not stream_active.is_set():
                display_message = "Waiting for video stream..."
            else:
                with frame_lock:
                    if latest_frame is not None:
                        frame_to_show = latest_frame.copy()
                    else:
                         display_message = "Stream active, waiting for frame..."

            # --- Display Frame or Message ---
            if frame_to_show is not None:
                cv2.imshow(win_name, frame_to_show)
            else:
                 placeholder = cv2.UMat(JPEG_WH[1]*2, JPEG_WH[0]*2, cv2.CV_8UC3, (40, 40, 40)).get() # Dark gray
                 if display_message:
                      cv2.putText(placeholder, display_message, (30, placeholder.shape[0] // 2),
                                  cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
                 cv2.imshow(win_name, placeholder)


            # --- Handle User Input / Window Events ---
            key = cv2.waitKey(30) & 0xFF # Check frequently
            if key == ord('q'):
                print("[Video Display] 'q' pressed. Initiating shutdown.")
                shutdown_flag.set()
                break

            if cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1:
                 print("[Video Display] Window closed by user. Initiating shutdown.")
                 shutdown_flag.set()
                 break

        except cv2.error as e:
            if "NULL window" in str(e) or "Invalid window handle" in str(e):
                 print("[Video Display] Window seems to have closed unexpectedly.")
                 shutdown_flag.set()
                 break
            else:
                 print(f"[Video Display OpenCV Error] {e}", file=sys.stderr)
                 time.sleep(0.5) # Avoid spamming errors
        except Exception as e:
            print(f"[Video Display Worker Error] {e}", file=sys.stderr)
            shutdown_flag.set() # Shutdown on unexpected errors
            break

    # --- Cleanup ---
    print("[Video Display] Closing window...")
    try:
        cv2.destroyWindow(win_name)
        cv2.waitKey(1)
    except Exception as e:
        print(f"[Video Display] Error destroying window: {e}", file=sys.stderr)
    print("[Video Display] Worker finished.")

# ----------------------------------------------------------------------------
#                 QUEUE / EVENT HELPERS (with shutdown check)
# ----------------------------------------------------------------------------

# queue_get_with_shutdown and event_wait_with_shutdown remain the same
def queue_get_with_shutdown(q: queue.Queue, timeout=1.0, poll=0.05):
    """ Gets from queue with timeout, checking shutdown flag periodically. """
    start_time = time.monotonic()
    while (time.monotonic() - start_time) < timeout:
        if shutdown_flag.is_set(): return None
        try:
            return q.get(timeout=poll)
        except queue.Empty:
            continue
    return None


def event_wait_with_shutdown(ev: threading.Event, timeout=None, poll=0.05):
    """ Waits for an event, checking shutdown flag periodically. Returns True if event set, False on timeout/shutdown."""
    start_time = time.monotonic()
    while not shutdown_flag.is_set():
        if ev.wait(timeout=poll):
            return True # Event was set
        current_time = time.monotonic()
        if timeout is not None and (current_time - start_time) >= timeout:
            return False # Event timed out
    return False # Shutdown occurred

# ----------------------------------------------------------------------------
#                                 MAIN LOOP (Restored Logic)
# ----------------------------------------------------------------------------
def main():
    # FIX: Declare usage of global variable at the START of the function
    global udp_sock

    print("[Main] Starting application...")
    main_start_time = time.monotonic()

    # --- Start Remote Scripts (SSH) ---
    if not start_remote_scripts():
        print("[Main] Critical Error: Failed to start remote scripts on RPi. Exiting.", file=sys.stderr)
        return 1

    # --- Initialize Video Capture ---
    print("[Main] Initializing video capture...")
    cap = cv2.VideoCapture(PI_VIDEO_STREAM_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("[Main] Video stream not immediately available, waiting 2s...")
        time.sleep(2)
        cap.release()
        cap = cv2.VideoCapture(PI_VIDEO_STREAM_URL, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            print(f"[Error] Cannot open video stream: {PI_VIDEO_STREAM_URL} after retry.", file=sys.stderr)
            try: send_drive_command("stop", 0, 0) # Use new signature
            except: pass # nosec
            stop_remote_scripts()
            # Check the global udp_sock correctly before closing
            if udp_sock:
                try:
                    udp_sock.close()
                except Exception as e:
                     print(f"[Cleanup] Error closing UDP socket during init fail: {e}")
                udp_sock = None # Mark as closed
            return 1

    print("[Main] Video capture opened.")
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 2) # Try to keep buffer small
    stream_active.set() # Assume active initially, frame_reader_thread will clear if needed

    # --- Start Local Threads ---
    print("[Main] Starting local threads...")
    threading.Thread(target=frame_reader_thread, args=(cap, PI_VIDEO_STREAM_URL), daemon=True).start()
    video_thread = None
    if ENABLE_VIDEO_WINDOW:
        video_thread = threading.Thread(target=video_display_worker, daemon=True)
        video_thread.start()

    time.sleep(1.0) # Allow threads to initialize

    # --- Main Control Loop Variables ---
    img_counter = 0
    narration_skip = 0 # Start narrating on first cycle

    print(f"[Main] Entering main control loop... (Startup time: {time.monotonic() - main_start_time:.2f}s)")
    try:
        # (The entire 'try' block with the main loop remains the same as the previous correct version)
        while not shutdown_flag.is_set():
            cycle_start_time = time.monotonic()
            # Increment cycle counter at the beginning to fix logging issue
            img_counter += 1
            print(f"\n--- Cycle {img_counter} ---")

            # --- Check Video Stream Health ---
            if not stream_active.wait(timeout=0.1): # Check if stream is marked active
                print("[Main] Video stream is not active. Waiting...")
                if shutdown_flag.wait(1.0): break
                # Don't increment cycle end time here, just skip cycle logic
                time.sleep(0.1) # Prevent tight loop if stream stays down
                continue # Skip rest of the cycle if stream inactive

            # --- Get Latest Frame ---
            frame = None
            with frame_lock:
                if latest_frame is not None: frame = latest_frame.copy()
            if frame is None:
                print("[Main] Waiting for frame...")
                if shutdown_flag.wait(0.1): break
                # Don't increment cycle end time here
                time.sleep(0.1) # Prevent tight loop
                continue # Skip cycle if no frame available yet

            # --- Decide Narration & Update Skip Counter ---
            narrate = (narration_skip == 0)
            # Reset to random(0..max) if narrating, else decrement.
            # Ensure skip doesn't go below 0 if MAX_NARRATION_SKIP is 0
            if narrate:
                 narration_skip = random.randint(0, MAX_NARRATION_SKIP)
            elif narration_skip > 0:
                 narration_skip -= 1

            print("[Main] Decision: Narrate" if narrate else f"[Main] Decision: Skip narration ({narration_skip} skips remaining)")

            # --- Start Analysis Worker ---
            # Clear previous result before starting new analysis
            try: _ = analysis_result_queue.get_nowait()
            except queue.Empty: pass
            threading.Thread(target=analysis_worker, args=(frame,), daemon=True).start()

            # --- Start Description Worker (if narrating) ---
            narration_playback_event = threading.Event() # Event specific to this cycle's narration
            if narrate:
                # Clear previous potential result before starting new description
                # (This is handled in the finally block of calculate_audio_duration now)
                threading.Thread(target=description_worker, args=(frame, narration_playback_event), daemon=True).start()
            else:
                # If not narrating, ensure the event doesn't block later waits indefinitely
                narration_playback_event.set()
                # FIX for Issue 3: Clear queue and put placeholder safely
                try:
                    # Attempt to clear any stale item first
                    try: _ = narration_info_queue.get_nowait()
                    except queue.Empty: pass
                    # Put placeholder info with a small timeout
                    narration_info_queue.put({'duration': 0.0, 'event': narration_playback_event}, timeout=0.5)
                except queue.Full:
                    # This should be less likely now, but log if it happens
                    print("[Main] Warning: Narration info queue full even when putting placeholder?", file=sys.stderr)


            # --- Get Navigation Command ---
            nav_cmd = queue_get_with_shutdown(analysis_result_queue, timeout=10.0) # Increased timeout slightly

            # --- Handle Navigation Command Result ---
            if nav_cmd is None or nav_cmd == "stop":
                if not shutdown_flag.is_set():
                    print("[Main] No valid navigation command received (or stop/timeout).")
                send_drive_command("stop", 0, 0) # Ensure stop is sent
                if nav_cmd is None and not shutdown_flag.is_set(): # Timeout case
                    print("[Main] Warning: Timeout receiving navigation command.")
                    # No continue here, let cycle end normally after stopping
                #else: # Stop command or shutdown signal
                    # break # Don't break here, let finally block handle shutdown cleanup
            else:
                # --- Valid Navigation Command Received ---
                print(f"[Main] Received Navigation Command: '{nav_cmd}'")

                # === Wait for Narration (if applicable) ===
                pre_move_wait_time = 0.0 # How long to wait *before* starting motors
                narration_info = None

                if narrate:
                    print("[Main] Waiting for narration info (duration calculation)...")
                    narration_info = None
                    wait_start_time = time.monotonic()
                    # Increased timeout for the *total* waiting period
                    queue_timeout = 25.0

                    # FIX: Loop until correct info received or timeout
                    while time.monotonic() - wait_start_time < queue_timeout:
                         # Use a shorter polling timeout for the actual get
                         current_info = queue_get_with_shutdown(narration_info_queue, timeout=1.0, poll=0.1)

                         if current_info is None:
                             # queue_get_with_shutdown timed out or shutdown signal received
                             if shutdown_flag.is_set():
                                 print("[Main] Shutdown detected while waiting for narration info.")
                                 break # Exit waiting loop
                             # Continue waiting if overall timeout not exceeded
                             continue

                         # Check if the event matches the current cycle's event
                         if current_info.get('event') is narration_playback_event:
                             print("[Main] Received correct narration info for this cycle.")
                             narration_info = current_info
                             break # Got the correct info, exit loop
                         else:
                             # Got info, but it's stale (from a previous cycle)
                             print("[Main] Discarding stale narration info from queue. Waiting for current cycle's info...")
                             # Loop continues to wait for the correct item (do nothing else here)

                    # Check if shutdown happened during the wait
                    if shutdown_flag.is_set():
                        break # Exit main loop if shutdown occurred

                    # After the loop, check if we got the info or timed out
                    if narration_info is None:
                        # Overall timeout occurred without getting correct info
                        if not shutdown_flag.is_set(): # Check flag again just in case
                            print(f"[Main] Warning: Timeout ({queue_timeout}s) waiting for *correct* narration info.")
                        narration_playback_event.set() # Ensure event is set if we proceed without info
                        pre_move_wait_time = 0.0 # Cannot calculate wait time
                    else:
                        # We have the correct narration_info, proceed to wait for playback start
                        print("[Main] Waiting for narration playback to *start*...")
                        playback_started = event_wait_with_shutdown(narration_playback_event, timeout=10.0)

                        if playback_started:
                            narration_duration = narration_info.get('duration', 0.0)
                            if narration_duration <= 0.0 and not shutdown_flag.is_set():
                                print(f"[Main] Warning: Narration duration calculated as {narration_duration:.2f}s. Proceeding without pre-wait.")
                                pre_move_wait_time = 0.0
                            else:
                                print(f"[Main] Playback started. Full Narration Duration: {narration_duration:.2f}s")
                                pre_move_wait_time = max(0.0, narration_duration - NARRATION_LEAD_TIME_S)
                                print(f"[Main] Calculated pre-movement wait: {pre_move_wait_time:.2f}s")
                        elif not shutdown_flag.is_set():
                            print("[Main] Warning: Timeout waiting for narration playback *start* signal.")
                            pre_move_wait_time = 0.0 # Proceed without wait if start signal times out

                # --- Execute Pre-Movement Wait (if needed) ---
                if pre_move_wait_time > 0:
                    print(f"[Main] Waiting {pre_move_wait_time:.2f}s before movement (allowing narration to play)...")
                    if shutdown_flag.wait(pre_move_wait_time):
                        print("[Main] Shutdown during pre-movement wait.")
                        break # Exit loop if shutdown during wait

                # === End of Narration Wait Logic ===

                # FIX for Issue 5: Check shutdown flag *before* sending move command
                if shutdown_flag.is_set():
                     print("[Main] Shutdown detected before sending move command.")
                     break

                # --- Determine Movement Parameters ---
                move_t = 0.0
                speed_l = 0 # Speed Left
                speed_r = 0 # Speed Right

                if nav_cmd == "forward":
                    move_t = FORWARD_DURATION
                    speed_l = FORWARD_SPEED_LEFT
                    speed_r = FORWARD_SPEED_RIGHT
                elif nav_cmd == "backward":
                    move_t = FORWARD_DURATION # Use same duration for backward
                    speed_l = BACKWARD_SPEED_LEFT
                    speed_r = BACKWARD_SPEED_RIGHT
                elif nav_cmd == "left" or nav_cmd == "right":
                    move_t = TURN_DURATION
                    speed_l = TURN_SPEED # Use single turn speed for left motor parameter
                    speed_r = TURN_SPEED # Pass same speed for right, RPi ignores it for turn format

                # --- Execute Movement ---
                if nav_cmd in ("forward", "backward"): print(f"[Main] Executing: '{nav_cmd}' for {move_t:.2f}s @ speeds L:{speed_l}, R:{speed_r}")
                elif nav_cmd in ("left", "right"): print(f"[Main] Executing: '{nav_cmd}' for {move_t:.2f}s @ speed {speed_l}")

                send_drive_command(nav_cmd, speed_l, speed_r) # Use new signature

                # --- Wait for Movement Duration ---
                if move_t > 0:
                    if shutdown_flag.wait(move_t):
                        print("[Main] Shutdown during movement.")
                        # Stop command will be sent in finally block
                        break # Exit loop

                # --- Stop Motors ---
                # Send explicit stop unless shutdown was triggered during movement wait
                if not shutdown_flag.is_set():
                    print("[Main] Movement time elapsed. Stopping motors.")
                    send_drive_command("stop", 0, 0) # Use new signature


            # --- Cycle End ---
            cycle_duration = time.monotonic() - cycle_start_time
            print(f"--- Cycle End (Duration: {cycle_duration:.2f}s) ---")

            if IMAGE_LIMIT and img_counter >= IMAGE_LIMIT:
                print(f"[Main] Reached image limit ({IMAGE_LIMIT}). Shutting down.")
                shutdown_flag.set()
                break

            # Small delay to prevent overly tight loops if everything finishes instantly
            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n[Main] KeyboardInterrupt caught in main loop. Shutting down.")
        if not shutdown_flag.is_set(): shutdown_flag.set()
    except Exception as e:
        print(f"\n[Main] UNEXPECTED ERROR in main loop: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        if not shutdown_flag.is_set(): shutdown_flag.set()
    finally:
        # --- Cleanup ---
        print("[Main] Starting cleanup sequence...")
        if not shutdown_flag.is_set(): shutdown_flag.set() # Ensure flag is set

        print("[Main] Sending final stop command...")
        stop_sent = False
        # REMOVED: global udp_sock (already declared at function start)
        for _ in range(3): # Try sending stop 3 times
            try:
                # FIX for Issue 6: Correct check for global UDP socket
                if udp_sock: # Check if the global socket object exists/is valid
                    send_drive_command("stop", 0, 0) # Use new signature
                    stop_sent = True
                    print("[Cleanup] Final stop command sent.")
                    break
                else:
                    print("[Cleanup] UDP socket not available or already closed.")
                    break # No point retrying if socket isn't there
            except Exception as e:
                print(f"[Cleanup] Error sending final stop (attempt {_ + 1}): {e}")
                time.sleep(0.1)
        if not stop_sent: print("[Main] Warning: Failed to send final stop command reliably.")

        stop_remote_scripts() # Stop RPi scripts & close SSH

        # Close the socket if it exists and hasn't been closed yet
        if udp_sock: # Check the global variable directly
            print("[Main] Closing UDP socket...")
            try:
                udp_sock.close()
            except Exception as e:
                 print(f"[Cleanup] Error closing UDP socket: {e}")
            udp_sock = None # Mark as closed

        # Wait for threads (optional but good practice)
        # Frame reader thread is daemon, will exit automatically
        if video_thread and video_thread.is_alive():
            print("[Main] Waiting for video display thread...")
            video_thread.join(timeout=2.0) # Wait max 2 seconds

        if ENABLE_VIDEO_WINDOW:
            print("[Main] Destroying OpenCV windows...")
            try:
                cv2.destroyAllWindows()
                cv2.waitKey(1) # Needs a small wait for windows to close properly
            except Exception as e:
                print(f"[Cleanup] Warning: Error destroying cv2 windows: {e}")

        print(f"[Main] Application shutdown complete. (Total runtime: {time.monotonic() - main_start_time:.2f}s)")

# The rest of the script remains the same...

if __name__ == "__main__":
    main()