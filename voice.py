import pyttsx3
import time

engine = pyttsx3.init()
voices = engine.getProperty('voices')

for index, voice in enumerate(voices):
    engine = pyttsx3.init()
    print(f"Voice {index}: {voice.name}")
    engine.setProperty('voice', voice.id)
    engine.say(f"This is voice {index}")
    engine.runAndWait()
    time.sleep(0.5)
