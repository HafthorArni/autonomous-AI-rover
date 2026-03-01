#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import RPi.GPIO as GPIO
import socket
import time
import sys
import traceback # For detailed error printing

# === Pin Configuration (BCM Mode) ===
# Make sure these match your physical wiring!
STBY = 26  # Standby pin for motor driver (HIGH = enabled)

# Motor A (e.g., Left Motor)
AIN1 = 9   # Motor A Direction Input 1
AIN2 = 10  # Motor A Direction Input 2
PWMA = 18  # Motor A Speed Control (PWM) - Must be HW PWM pin (GPIO12, GPIO13, GPIO18, GPIO19)

# Motor B (e.g., Right Motor)
BIN1 = 16  # Motor B Direction Input 1
BIN2 = 20  # Motor B Direction Input 2
PWMB = 12  # Motor B Speed Control (PWM) - Must be HW PWM pin

# === Parameters ===
PWM_FREQ = 400 # PWM Frequency in Hz (adjust if needed, e.g., 100, 500, 1000)
UDP_IP = "0.0.0.0" # Listen on all available interfaces
UDP_PORT = 5005    # Port for receiving commands

# === Global Variables ===
pwm_A = None
pwm_B = None
sock = None
addr = None # To store sender address for logging

# === GPIO Setup Function ===
def setup_gpio():
    """Initializes GPIO pins and PWM."""
    global pwm_A, pwm_B
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False) # Disable warnings about channel usage

        # Define all pins used
        motor_pins = [STBY, AIN1, AIN2, PWMA, BIN1, BIN2, PWMB]

        # Setup pins as outputs and initialize low
        for pin in motor_pins:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)

        # Initialize PWM (Hardware PWM recommended for pins 12, 13, 18, 19)
        pwm_A = GPIO.PWM(PWMA, PWM_FREQ)
        pwm_B = GPIO.PWM(PWMB, PWM_FREQ)
        pwm_A.start(0) # Start PWM with 0% duty cycle
        pwm_B.start(0)

        GPIO.output(STBY, GPIO.LOW) # Keep motors disabled initially
        print("[GPIO] Setup successful.")
        return True

    except Exception as e:
        print(f"[GPIO Error] Failed to initialize GPIO or PWM: {e}", file=sys.stderr)
        traceback.print_exc()
        # Attempt cleanup if possible
        try: GPIO.cleanup()
        except: pass # nosec
        return False

# === Motor Control Functions ===
def motor_control(motor_a_dir, motor_b_dir, speed_a, speed_b):
    """
    Low-level motor control. Sets direction and speed for both motors.
    dir: 1=forward, -1=backward, 0=stop/brake
    speed_a, speed_b: 0-100 duty cycle
    """
    # Clamp speeds individually
    try:
        speed_a_clamped = max(0, min(100, int(speed_a)))
        speed_b_clamped = max(0, min(100, int(speed_b)))
    except ValueError:
        print(f"[Motor Ctrl Error] Invalid speed types: A='{speed_a}', B='{speed_b}'. Setting to 0.", file=sys.stderr)
        speed_a_clamped = 0
        speed_b_clamped = 0
        motor_a_dir = 0 # Force stop if speeds are invalid
        motor_b_dir = 0

    # Enable driver only if movement is intended
    if motor_a_dir != 0 or motor_b_dir != 0:
        GPIO.output(STBY, GPIO.HIGH)
    else:
        # If stopping, speeds should be 0 anyway, but ensure driver is off eventually
        speed_a_clamped = 0
        speed_b_clamped = 0
        # Note: motor_stop() handles disabling STBY properly

    # Motor A Direction (Left)
    if motor_a_dir == 1:    # Forward
        GPIO.output(AIN1, GPIO.HIGH)
        GPIO.output(AIN2, GPIO.LOW)
    elif motor_a_dir == -1: # Backward
        GPIO.output(AIN1, GPIO.LOW)
        GPIO.output(AIN2, GPIO.HIGH)
    else:                   # Stop/Brake
        GPIO.output(AIN1, GPIO.LOW) # Or HIGH/HIGH for brake depending on driver
        GPIO.output(AIN2, GPIO.LOW)

    # Motor B Direction (Right)
    if motor_b_dir == 1:    # Forward
        GPIO.output(BIN1, GPIO.HIGH)
        GPIO.output(BIN2, GPIO.LOW)
    elif motor_b_dir == -1: # Backward
        GPIO.output(BIN1, GPIO.LOW)
        GPIO.output(BIN2, GPIO.HIGH)
    else:                   # Stop/Brake
        GPIO.output(BIN1, GPIO.LOW) # Or HIGH/HIGH for brake
        GPIO.output(BIN2, GPIO.LOW)

    # Set Speed (Duty Cycle)
    pwm_A.ChangeDutyCycle(speed_a_clamped)
    pwm_B.ChangeDutyCycle(speed_b_clamped)
    # Optional: Add print here if you want to see the final applied speeds and directions
    # print(f"[Motor Ctrl] Dir A:{motor_a_dir} B:{motor_b_dir} | Speed A:{speed_a_clamped} B:{speed_b_clamped}")


def motor_stop():
    """Stops both motors gracefully and disables the driver chip."""
    print("[Motor] Called: Stop")
    # Set duty cycles to 0 first
    if pwm_A: pwm_A.ChangeDutyCycle(0)
    if pwm_B: pwm_B.ChangeDutyCycle(0)
    # Set direction pins to low (coast or brake depending on driver)
    GPIO.output(AIN1, GPIO.LOW)
    GPIO.output(AIN2, GPIO.LOW)
    GPIO.output(BIN1, GPIO.LOW)
    GPIO.output(BIN2, GPIO.LOW)
    # Disable motor driver chip (important for some drivers like TB6612FNG)
    time.sleep(0.05) # Short delay to ensure commands are processed before standby
    GPIO.output(STBY, GPIO.LOW)


# === Command Handling ===
def handle_command(data):
    """
    Parses command and executes the corresponding motor action via motor_control/motor_stop.
    Expects 'forward:speed_left:speed_right' or 'backward:speed_left:speed_right'.
    Expects 'left:speed', 'right:speed', or 'stop:0'.
    """
    command_str = "" # Initialize for error message
    try:
        command_str = data.decode('utf-8').strip().lower()
        if not command_str:
            print("[UDP] Received empty packet. Ignoring.")
            return # Ignore empty packets

        print(f"[UDP] Received raw: '{command_str}' from {addr}")

        parts = command_str.split(':')
        cmd = parts[0]
        num_parts = len(parts)

        speed_left = 0
        speed_right = 0

        # --- Parse based on command ---
        if cmd == "stop":
            if num_parts >= 1: # Allow 'stop' or 'stop:0' etc.
                 motor_stop()
            else: # Should not happen if split works
                 print(f"[WARN] Invalid stop command format: '{command_str}'. Stopping anyway.")
                 motor_stop()
            return # Finished handling stop

        elif cmd in ("forward", "backward"):
            if num_parts == 3:
                try:
                    speed_left = int(parts[1])
                    speed_right = int(parts[2])
                    # Basic validation (motor_control clamps further)
                    if not (0 <= speed_left <= 100 and 0 <= speed_right <= 100):
                        print(f"[WARN] Speeds out of range (0-100) in '{command_str}'. Will be clamped.")
                    print(f"[UDP] Parsed '{cmd}' L:{speed_left} R:{speed_right}")
                except ValueError:
                    print(f"[WARN] Invalid speed values in '{command_str}'. Expected integers. Stopping.")
                    motor_stop()
                    return
            else:
                print(f"[WARN] Invalid format for '{cmd}': '{command_str}'. Expected 'command:speed_left:speed_right'. Stopping.")
                motor_stop()
                return

            # Execute forward/backward directly using motor_control
            if cmd == "forward":
                motor_control(1, 1, speed_left, speed_right) # Motor A Fwd, Motor B Fwd
            else: # backward
                motor_control(-1, -1, speed_left, speed_right) # Motor A Bwd, Motor B Bwd

        elif cmd in ("left", "right"):
             if num_parts == 2:
                try:
                    turn_speed = int(parts[1])
                    # Clamp turn speed here (0-100)
                    turn_speed = max(0, min(100, turn_speed))
                    print(f"[UDP] Parsed '{cmd}' @ {turn_speed}%")
                except ValueError:
                    print(f"[WARN] Invalid speed value in '{command_str}'. Expected integer. Stopping.")
                    motor_stop()
                    return
             else:
                print(f"[WARN] Invalid format for '{cmd}': '{command_str}'. Expected 'command:speed'. Stopping.")
                motor_stop()
                return

             # Execute turns directly using motor_control (pivot turn)
             if cmd == "left":
                 # Left motor backward, Right motor forward
                 motor_control(-1, 1, turn_speed, turn_speed)
             else: # right
                 # Left motor forward, Right motor backward
                 motor_control(1, -1, turn_speed, turn_speed)

        else:
            print(f"[WARN] Unknown command received: '{cmd}' in '{command_str}'. Stopping motors.")
            motor_stop()

    except UnicodeDecodeError:
        print(f"[WARN] Received non-UTF8 data from {addr}. Ignoring.")
    except IndexError:
        print(f"[ERROR] Malformed command string '{command_str}' led to index error. Stopping.")
        traceback.print_exc()
        motor_stop()
    except Exception as e:
        print(f"[ERROR] Unexpected error handling command '{command_str}' from {addr}: {e}", file=sys.stderr)
        traceback.print_exc() # Print full traceback for debugging
        motor_stop() # Stop motors if any error occurs during handling


# === UDP Server Setup ===
def setup_socket():
    """Binds the UDP socket to listen for commands."""
    global sock
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((UDP_IP, UDP_PORT))
        # Set a timeout so the loop doesn't block indefinitely
        # Allows KeyboardInterrupt to be caught more reliably if needed
        sock.settimeout(1.0) # Check for data every 1.0 seconds
        print(f"[INFO] Motor control server socket bound to UDP {UDP_IP}:{UDP_PORT}")
        return True
    except socket.error as e:
        print(f"[Socket Error] Failed to bind UDP socket {UDP_IP}:{UDP_PORT} : {e}", file=sys.stderr)
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"[Socket Error] Unexpected error setting up socket: {e}", file=sys.stderr)
        traceback.print_exc()
        return False

# === Cleanup Function ===
def cleanup():
    """Stops motors, PWM, and cleans up GPIO resources."""
    print("\n[INFO] Initiating cleanup...")
    print("[INFO] Stopping motors...")
    try:
        motor_stop() # Ensure motors are stopped and driver disabled
    except Exception as e:
        print(f"[Cleanup Warn] Error stopping motors during cleanup: {e}")

    print("[INFO] Stopping PWM...")
    global pwm_A, pwm_B
    try:
        if pwm_A: pwm_A.stop()
        if pwm_B: pwm_B.stop()
    except Exception as e:
        print(f"[Cleanup Warn] Error stopping PWM during cleanup: {e}")

    print("[INFO] Cleaning up GPIO pins...")
    try:
        GPIO.cleanup()
    except Exception as e:
        print(f"[Cleanup Warn] Error during GPIO cleanup: {e}")

    print("[INFO] Closing UDP socket...")
    global sock
    if sock:
        try:
            sock.close()
            print("[INFO] Socket closed.")
        except Exception as e:
            print(f"[Cleanup Warn] Error closing socket during cleanup: {e}")

    print("[INFO] Cleanup complete. Exiting.")

# === Main Execution ===
if __name__ == "__main__":
    print("--- RPi Motor Control Script ---")
    # Ensure GPIO and Socket setup succeed before proceeding
    if not setup_gpio():
        print("[FATAL] GPIO setup failed. Exiting.")
        sys.exit(1)

    if not setup_socket():
        print("[FATAL] Socket setup failed. Running cleanup.")
        cleanup() # Attempt cleanup even if socket failed
        sys.exit(1)

    print("[INFO] Initialization complete. Waiting for commands...")
    addr = ('N/A', 0) # Initialize addr

    try:
        while True: # Main Listening Loop
            try:
                # Wait for data with timeout
                data, current_addr = sock.recvfrom(1024) # Buffer size 1024 bytes
                # Only update address if data is received
                addr = current_addr
                handle_command(data)
            except socket.timeout:
                # This is expected when no commands are sent for 1 second
                # You could add logic here, e.g., auto-stop motors if no command for X seconds
                # print("[DEBUG] Socket timeout.") # Optional debug
                pass # Continue loop
            except Exception as e:
                # Catch other potential errors during recvfrom or handle_command
                print(f"[ERROR] An unexpected error occurred in the main loop: {e}")
                traceback.print_exc()
                motor_stop() # Stop motors as a safety measure
                time.sleep(1) # Avoid busy-looping on continuous errors

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt received. Shutting down...")
    finally:
        # This block executes on normal exit, KeyboardInterrupt, or unhandled exceptions
        cleanup() # Ensure cleanup runs reliably
