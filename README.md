# Go2 Data Collection — User Guide

Record with the robot's round screen. Back in the lab, copy the data to your
laptop over one Ethernet cable.

![Screen states](docs/screen_states.png)

## Before your first session

On your laptop, once:

1. Install Python 3.8 or newer.
2. Download this repository:
   ```
   git clone https://github.com/yuzhench/go2_data_collection.git
   ```

There is nothing else to install.

## 1. Drive the robot

The robot is driven with the Unitree handheld controller.

**Power on**

1. Lay the robot flat on the ground with all four feet touching the floor.
   Keep people and objects 2 m away.
2. On the battery, press the power button once, then press and hold it until
   the robot starts. It stands up by itself within a minute.
3. Turn the controller on.

**Sticks**

| Stick | Push forward / back | Push left / right |
|---|---|---|
| Left | Walk forward / backward | Step sideways |
| Right | Tilt the body up / down | Turn left / right |

**Buttons**

"Hold" means keep the first button pressed while you click the second.

| To do this | Press |
|---|---|
| **Emergency stop** (motors go limp, the robot sinks down) | hold **L2** + **B** |
| Lock the stance / lie down (press twice to stand again) | hold **L2** + **A** |
| Unlock and walk | **START** |
| Run | hold **L2** + **START** |
| Stand up after a fall | hold **L2** + **X** |
| Obstacle avoidance on | **X** |
| Obstacle avoidance off | hold **Y** for 3 seconds |

Learn the emergency stop before you start. If the robot does anything you
did not intend, use it.

**Power off:** lie the robot down (hold **L2** + **A**), then press the
battery button once and press and hold it again.

## 2. Record

1. Turn the robot on and wait about a minute.
2. Long-press the side button of the screen to turn it on. It shows **IDLE**.
3. Check **FREE** on the right. One minute of recording uses about 1.3 GB.
4. **Start:** hold the centre circle for 1 second, until the white ring closes.
   The timer stays at `00:00` for 2 seconds; data is kept from then on.
5. While recording, all five circles should be green:
   `CAM 30`, `LIDAR 15`, `IMU` / `JOINT` / `FOOT 500`.
6. **Stop:** hold the centre circle for 1 second again and wait for **IDLE**.

A flashing red **!** means a sensor stopped sending data. Stop, check the
camera cable, and start a new recording.

## 3. Copy your data to your laptop

Do this the same day. The next person needs the disk space.

1. Keep the robot on and lying down (hold **L2** + **A**). Plug an Ethernet cable from your laptop
   into the port on the robot's back. The screen shows **PC LINKED**.
2. In the downloaded folder, run:
   ```
   cd go2_data_collection
   python go2_offload.py
   ```
   (`python3` on macOS and Linux.) A page opens in your browser with every
   recording on the robot.

   ![Session list](docs/session_list.png)

3. Tick your recordings and click **Copy selected to this computer**.
   The robot's screen shows **EXPORT**, then **VERIFY**. One hour of
   recording takes about 15 minutes.
4. The page then asks what to delete from the robot. Your copied recordings
   are already ticked. Click **Delete selected from the robot**.
5. When the screen shows **DONE – UNPLUG OK**, unplug the cable and turn the
   robot off.

Your data is in the `Go2Data` folder in your home directory. To put it
somewhere else, run `python go2_offload.py --dest <folder>`.

Two rules:

- Only delete your own recordings.
- A recording marked **not copied** exists only on the robot. Deleting it is
  permanent.

## 4. Label your data

Coming soon.

## If something is wrong

| You see | Do this |
|---|---|
| Robot does something unexpected | Emergency stop: hold **L2** + **B**. |
| Screen is black | Long-press the side button of the screen. |
| **NO LINK** on the screen | The robot is still starting; wait a minute. If it stays, restart the robot. |
| **ERROR** after holding to start | The camera is not detected. Re-plug the camera cable and try again. |
| Red **!** while recording | A sensor dropped out. Stop and start a new recording. |
| Page says *Robot not connected* | Check that the robot is on and the cable is plugged in at both ends. |
| Copy stops halfway | Plug the cable back in. Copying continues from where it stopped. |
| Page says *Something went wrong* | Nothing was deleted. Click **Back to the session list** and copy again; finished files are skipped. |
