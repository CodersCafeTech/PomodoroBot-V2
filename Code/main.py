import os
import asyncio
import pvporcupine
import pyaudio
import struct
import speech_recognition as sr
import re
import pytz
from datetime import datetime, timedelta
from gtts import gTTS
import tkinter as tk
from tkinter import Label, font
import cv2
from PIL import Image, ImageTk
import subprocess
import threading
import queue
import sys
from viam.robot.client import RobotClient
from viam.services.generic import Generic
from viam.components.sensor import Sensor
from chat_service_api import Chat
from gpiozero import Button

# Configuration
os.environ["DISPLAY"] = ":0"
BUTTON_PIN = 17
WAKE_WORD = "hey google"
VIDEO_PATH = "animations/blink.mp4"

# Global state
video_queue = queue.Queue()
message_queue = queue.Queue()
button_queue = queue.Queue()
upcoming_events = []
notification_window = None
app = None

INTENT_PATTERNS = [
    {"name": "set_pomodoro", "pattern": r"(?:start|begin) (?:pomodoro|timer) for (\d+) minutes"},
    {"name": "show_calendar", "pattern": r"(?:show|display) (?:calendar|events)"},
    {"name": "dismiss", "pattern": r"(?:dismiss|close) (?:notification|window)"},
    {"name": "set_timer", "pattern": r"(?:set|start) (?:a )?timer for (\d+) (?:minutes|seconds)"},
    {"name": "set_alarm", "pattern": r"(?:set|schedule) an? alarm for (\d{1,2}):(\d{2})"},
]

class VideoApp:
    def __init__(self, root):
        print("[DEBUG] Initializing VideoApp")
        self.root = root
        self.video_source = VIDEO_PATH
        self.cap = cv2.VideoCapture(self.video_source)
        self.timer_running = False
        self.video_playing = True
        self.root.attributes('-fullscreen', True)
        self.root.bind('<Escape>', lambda e: self.root.destroy())
        # Initialize Orbitron fonts
        self.orbitron_font = font.Font(family="Orbitron", size=200, weight="bold")
        self.orbitron_font_small = font.Font(family="Orbitron", size=35, weight="normal")
        self.video_label = Label(self.root)
        self.video_label.pack(fill=tk.BOTH, expand=True)
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.update_video()

    def set_video_source(self, new_source):
        if new_source != self.video_source:
            print(f"[INFO] Changing video source to: {new_source}")
            self.video_source = new_source
            self.cap.release()
            self.cap = cv2.VideoCapture(new_source)

    def update_video(self):
        if not self.video_playing or not self.cap.isOpened():
            self.root.after(100, self.update_video)
            return
        ret, frame = self.cap.read()
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame = cv2.resize(frame, (self.screen_width, self.screen_height))
            img = ImageTk.PhotoImage(Image.fromarray(frame))
            self.video_label.config(image=img)
            self.video_label.image = img
        else:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self.root.after(30, self.update_video)

    def display_message(self, message):
        print(f"[INFO] Displaying message: {message}")
        self.video_playing = False
        self.video_label.config(image="", text=message, fg="white", bg="black")
        target_font = self.orbitron_font
        self.video_label.config(text=message, font=target_font)

    def start_pomodoro_timer(self, duration):
        print(f"[INFO] Starting Pomodoro timer for {duration} minutes")
        self.timer_running = True
        self.display_message(f"{duration}")
        self.countdown(duration * 60)

    def countdown(self, remaining):
        if remaining <= 0:
            #self.display_message("Time's up!")
            self.root.after(1000, self.reset_video)
            return
        self.video_label.config(text=f"{remaining // 60}:{remaining % 60:02d}")
        self.root.after(1000, self.countdown, remaining - 1)

    def reset_video(self):
        self.video_playing = True
        #asyncio.create_task(self.monitor_sensors())

class VoiceAssistant:
    def __init__(self, robot):
        print("[DEBUG] Initializing VoiceAssistant")
        self.robot = robot
        self.llm = None
        self.porcupine = pvporcupine.create(
            access_key="UA3PcPN/0fbqTt3leckt4xxxxxxxxxxxxxxxxxxxxx6XI/MJrv0g==",
            keywords=[WAKE_WORD]
        )
        self.pa = pyaudio.PyAudio()
        self.audio_stream = self.pa.open(
            rate=self.porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=self.porcupine.frame_length,
            stream_callback=self._audio_callback
        )
        self.wake_word_event = asyncio.Event()
        self.audio_stream.start_stream()
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()

    async def initialize(self):
        print("[DEBUG] Initializing LLM service")
        self.llm = Chat.from_robot(self.robot, name="LLM")
        print("[INFO] LLM service initialized")

    def _audio_callback(self, in_data, frame_count, time_info, status):
        if status:
            print(f"[ERROR] Audio callback status: {status}")
        pcm = struct.unpack_from("h" * self.porcupine.frame_length, in_data)
        keyword_index = self.porcupine.process(pcm)
        if keyword_index >= 0:
            print("[INFO] Wake word detected")
            self.wake_word_event.set()
        return (in_data, pyaudio.paContinue)

    async def wait_for_wake_word(self):
        await self.wake_word_event.wait()
        self.wake_word_event.clear()
        return True

    async def process_voice_command(self, app):
        audio = self._record_audio()
        if not audio:
            return
        text = self._speech_to_text(audio)
        if not text:
            return
        print(f"[INFO] Recognized text: {text}")
        intent = await self._get_intent(text)
        if intent:
            await self._handle_intent(intent, app)
        else:
            response = await self.llm.chat(text)
            speak_tts(response)

    def _record_audio(self):
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source)
            try:
                return self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
            except sr.WaitTimeoutError:
                return None

    def _speech_to_text(self, audio):
        try:
            return self.recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            print(f"[ERROR] Speech recognition error: {e}")
            return None

    def _extract_intent_regex(self, text):
        for intent in INTENT_PATTERNS:
            match = re.search(intent["pattern"], text, re.IGNORECASE)
            if match:
                return f"{intent['name']}({', '.join(match.groups())})"
        return None

    async def _extract_intent_llm(self, text):
        prompt = f"Extract intent from: '{text}' using format intent_name(parameters)"
        response = await self.llm.chat(prompt)
        match = re.search(r"(\w+)\((.*?)\)", response)
        return match.group(0) if match else None

    async def _get_intent(self, text):
        return self._extract_intent_regex(text) or await self._extract_intent_llm(text)

    async def _handle_intent(self, intent, app):
        try:
            function, args = re.match(r"(\w+)\((.*)\)", intent).groups()
            args = [a.strip() for a in args.split(',')] if args else []
            if function == "set_pomodoro" and args:
                app.start_pomodoro_timer(int(args[0]))
                speak_tts(f"Starting {args[0]} minute timer")
            elif function == "dismiss":
                close_notification(notification_window, app)
                speak_tts("Notification dismissed")
            elif function == "show_calendar":
                events = await get_events_and_check_alerts(self.robot)
                speak_tts(f"{len(events)} upcoming events")
        except Exception as e:
            print(f"[ERROR] Intent handling error: {e}")

def speak_tts(text):
    print(f"[INFO] TTS: {text}")
    try:
        tts = gTTS(text=text, lang='en')
        tts.save("response.mp3")
        prev_video = app.video_source 
        app.set_video_source("animations/speaking.mp4")
        def play_audio():
            subprocess.run(["mpg321", "-q", "response.mp3"])
            # Restore default animation after audio finishes
            app.set_video_source(prev_video)

        threading.Thread(target=play_audio, daemon=True).start()
    except Exception as e:
        print(f"[ERROR] TTS failed: {e}")

def create_meeting_notification(meeting_name, start_time, app):
    global notification_window
    if notification_window and notification_window.winfo_exists():
        notification_window.destroy()
    notification_window = tk.Toplevel()
    notification_window.attributes('-fullscreen', True)
    notification_window.configure(bg="black")
    orbitron_font = font.Font(family="Orbitron", size=40)
    Label(notification_window, text="Meeting Soon", font=orbitron_font, fg="white", bg="black").pack(pady=20)
    Label(notification_window, text=meeting_name, font=orbitron_font, fg="white", bg="black").pack(pady=10)
    Label(notification_window, text=start_time, font=orbitron_font, fg="white", bg="black").pack(pady=10)
    tk.Button(notification_window, text="Dismiss", font=orbitron_font, command=lambda: close_notification(notification_window, app)).pack(pady=20)

def close_notification(window, app):
    if window and window.winfo_exists():
        window.destroy()
    app.set_video_source(VIDEO_PATH)

async def monitor_sensors(robot, app):
    prev_video = None
    while True:
        try:
            ens160 = Sensor.from_robot(robot, "ENS160")
            temt6000 = Sensor.from_robot(robot, "TEMT6000")
            eco2 = (await ens160.get_readings()).get("eCO2", 0)
            light = (await temt6000.get_readings()).get("light_intensity", 100)
            new_video = VIDEO_PATH
            if 500 <= eco2 < 750:
                new_video = "animations/yellow.mp4"
            elif eco2 >= 750:
                new_video = "animations/red.mp4"
            if light < 10:
                new_video = "animations/black.mp4"
            if new_video != prev_video:
                app.root.after(0, app.set_video_source, new_video)
                prev_video = new_video
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[ERROR] Sensor error: {e}")
            await asyncio.sleep(5)

async def get_events_and_check_alerts(robot):
    try:
        calendar = Generic.from_robot(robot, "Calendar")
        tz = pytz.timezone('Asia/Kolkata')
        now = datetime.now(tz)
        result = await calendar.do_command({
            "get_events": {
                "time_min": now.isoformat(),
                "time_max": (now + timedelta(minutes=15)).isoformat()
            }
        })
        return result.get("events", [])
    except Exception as e:
        print(f"[ERROR] Calendar error: {e}")
        return []

async def check_for_upcoming_meetings(robot, app):
    while True:
        events = await get_events_and_check_alerts(robot)
        for event in events:
            if event not in upcoming_events:
                app.root.after(0, create_meeting_notification, event.get("summary", "Meeting"), event.get("start"), app)
                upcoming_events.append(event)
        await asyncio.sleep(60)

async def handle_voice_commands(assistant, app):
    while True:
        await assistant.wait_for_wake_word()
        asyncio.create_task(assistant.process_voice_command(app))

async def connect():
    opts = RobotClient.Options.with_api_key(
        api_key='cscny47yivxxxxxxxxxxxiswt6chzyzca',
        api_key_id='e67c7cd5-xxxx-xxxx-xxxx-f5ddd4380abe'
    )
    return await RobotClient.at_address('pomodoro-main.xxxxxxxxxxx.viam.cloud', opts)

async def main():
    robot = await connect()
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry("{0}x{1}+0+0".format(
        root.winfo_screenwidth(),
        root.winfo_screenheight()
    ))
    root.attributes('-fullscreen', True)
    global app
    app = VideoApp(root)
    app.set_video_source(VIDEO_PATH)
    assistant = VoiceAssistant(robot)
    await assistant.initialize()
    
    # Create async tasks
    tasks = [
        asyncio.create_task(monitor_sensors(robot, app)),
        asyncio.create_task(check_for_upcoming_meetings(robot, app)),
        asyncio.create_task(handle_voice_commands(assistant, app)),
    ]
    
    # Run Tkinter and asyncio together
    while True:
        root.update()
        await asyncio.sleep(0.01)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        sys.exit(0)