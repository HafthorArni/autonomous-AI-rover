# Autonomous AI Rover

https://github.com/user-attachments/assets/e9924546-faa8-4ec5-9863-6f56c97cd00c



An autonomous, AI-powered rover built with a Raspberry Pi. The rover utilizes a "Brain and Brawn" architecture: an external PC acts as the "brain" to process video streams through OpenAI's GPT-4o-mini Vision model to navigate, while audibly narrating what it sees using ElevenLabs TTS. The Raspberry Pi acts purely as the "body," streaming video and executing motor commands.

## Architecture

* **`PC_brain/brain.py`:** The control center. It connects to the Raspberry Pi via SSH to automatically launch the camera stream and the motor listener. It processes incoming video frames, asks OpenAI for navigation decisions (forward, left, right, backward), and streams personality-driven audio narration. It sends movement commands to the Pi via UDP.
* **`RPI_body/body.py`:** The physical hardware controller. It runs on the Raspberry Pi, streaming h264 video over TCP using the native `libcamera-vid` command. It also runs a Python UDP server that converts incoming speed commands from the PC into hardware PWM signals for the motor driver.

## Repository Structure

```text
autonomous-AI-rover/
├── .env                    # (User created - excluded from version control)
├── .gitignore
├── README.md
├── PC_brain/
│   ├── brain.py            # Main AI & control loop
│   └── requirements.txt    # PC Python dependencies
├── RPI_body/
│   ├── body.py             # Raspberry Pi UDP motor server
│   └── requirements.txt    # Raspberry Pi Python dependencies
└── camera/                 # Arducam Pivariety driver installation scripts

```

## Hardware Requirements

* **The Body:** Raspberry Pi (with Wi-Fi connection, running **Raspbian GNU/Linux 11 Bullseye**).
* **Vision:** Arducam Pivariety Camera Module (or supported libcamera module).
* **Mobility:** DC Motors in a differential drive setup. (I used the ch n20 3 dc motor)
* **Motor Driver:** e.g., L298N or TB6612FNG connected to Pi GPIO. (I used the Dual TB6612FNG (1A))
* **Power:** a battery pack connected to a buck converter connected directly to the 5V input on the RPI
* **The Brain:** A separate PC (Windows/Mac/Linux) to run the AI processing.

## Setup Instructions

### 1. Raspberry Pi (Body) Setup

The Raspberry Pi requires specific boot configurations and kernel drivers to operate the camera and hardware PWM.

**Step 1: System Configuration**
You must enable I2C and allocate GPU memory. Open your boot configuration:

```bash
sudo nano /boot/config.txt

```

Ensure the following lines are present at the bottom of the file:

```text
gpu_mem=128
dtparam=i2c_vc=on
dtparam=i2c_arm=on
dtoverlay=arducam-pivariety

```

Reboot your Raspberry Pi to apply the changes:

```bash
sudo reboot

```

**Step 2: Camera Driver Installation**
The Arducam Pivariety camera requires a specific kernel driver installation script. Navigate to the camera directory and make the script executable:

```bash
cd camera/
chmod +x install_pivariety_pkgs.sh

```

Run the autodetect and install command:

```bash
./install_pivariety_pkgs.sh -d

```

Reboot the Pi once the installation is complete.

**Step 3: Python Dependencies**
Ensure your Raspberry Pi has the required packages for GPIO and camera control installed via the body requirements file:

```bash
pip install -r RPI_body/requirements.txt

```

*(Note: Essential packages in this file include `RPi.GPIO` for hardware PWM motor control, `picamera2`, `v4l2-python3`, `numpy`, and `gpiozero`)*.

### 2. PC (Brain) Setup

1. Clone this repository to your PC.
2. Install the required Python packages:

```bash
pip install -r PC_brain/requirements.txt

```

3. Create a new file named exactly `.env` in the root folder of the project.
4. Add your API keys and Raspberry Pi credentials to the `.env` file:

```text
# API Keys
OPENAI_API_KEY=your_openai_api_key_here
ELEVENLABS_API_KEY=your_elevenlabs_api_key_here

# Raspberry Pi Configuration
RPI_IP=192.168.1.37
RPI_USER=rover
RPI_PASSWORD=your_rpi_password

```

*(Note: The rover's voice personality can be changed by modifying the `VOICE_ID` variable directly inside `PC_brain/brain.py`)*

## Usage

Simply run the brain script on your PC:

```bash
python PC_brain/brain.py

```

The script will automatically SSH into the Raspberry Pi, start the background video and motor services, open a live video feed window on your PC, and begin autonomous AI navigation and narration.

**To stop the rover:** Press `q` while focused on the video window, or press `Ctrl+C` in the terminal to safely shut down the motors and close network connections.
