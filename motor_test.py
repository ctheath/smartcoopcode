import RPi.GPIO as GPIO
import time

# Pin setup
IN1 = 17
IN2 = 27
REED_OPEN = 22
REED_CLOSED = 23
OPEN_STOP_MODE = "brake_hold"
CLOSE_STOP_MODE = "coast"
BRAKE_PULSE_SECONDS = 1.0

GPIO.setmode(GPIO.BCM)
GPIO.setup([IN1, IN2], GPIO.OUT)
GPIO.setup([REED_OPEN, REED_CLOSED], GPIO.IN, pull_up_down=GPIO.PUD_UP)

def coast_motor():
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.LOW)

def brake_motor():
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.HIGH)

def stop_motor(mode):
    if mode == "brake_hold":
        brake_motor()
    elif mode == "brake_pulse":
        brake_motor()
        time.sleep(BRAKE_PULSE_SECONDS)
        coast_motor()
    elif mode == "coast":
        coast_motor()
    else:
        raise ValueError(f"Unsupported stop mode: {mode}")

def open_door():
    print("Opening door...")
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.LOW)
    while GPIO.input(REED_OPEN):  # Loop until open reed triggered (LOW = aligned)
        time.sleep(0.1)
    stop_motor(OPEN_STOP_MODE)
    print("Door opened.")

def close_door():
    print("Closing door...")
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.HIGH)
    while GPIO.input(REED_CLOSED):  # Loop until closed reed triggered
        time.sleep(0.1)
    stop_motor(CLOSE_STOP_MODE)
    print("Door closed.")

try:
    while True:
        cmd = input("Type 'o' to open, 'c' to close, or 'q' to quit: ")
        if cmd == 'o':
            open_door()
        elif cmd == 'c':
            close_door()
        elif cmd == 'q':
            break
        else:
            print("Invalid input.")
finally:
    coast_motor()
    GPIO.cleanup()
