#!/usr/bin/env python3

#####################################################
##          librealsense D4xx to MAVLink           ##
#####################################################
# This script assumes pyrealsense2.[].so file is found under the same directory as this script
# Install required packages: 
#   pip install pyrealsense2
#   pip install transformations
#   pip install dronekit
#   pip install apscheduler
#   pip install -i https://pypi.anaconda.org/sklam/simple llvmlite
#   pip install numba

# Set the path for IDLE
import sys
sys.path.append("/usr/local/lib/")

# Set MAVLink protocol to 2.
import os
os.environ["MAVLINK20"] = "1"

# Import the libraries
import pyrealsense2 as rs
import numpy as np
import transformations as tf
import math as m
import time
import argparse
import threading
from time import sleep
from apscheduler.schedulers.background import BackgroundScheduler
from dronekit import connect, VehicleMode
from pymavlink import mavutil

from numba import njit              # pip install numba, or use anaconda to install

# in order to import cv2 under python3 when you also have ROS installed
sys.path.remove('/opt/ros/kinetic/lib/python2.7/dist-packages') 
sys.path.append('~/anaconda3/lib/python3.7/site-packages') # Remove if not applicable to your system
import cv2

######################################################
##        Depth parameters - reconfigurable         ##
######################################################
# Sensor-specific parameter, for D435: https://www.intelrealsense.com/depth-camera-d435/
STREAM_TYPE = [rs.stream.depth, rs.stream.color]  # rs2_stream is a types of data provided by RealSense device
FORMAT      = [rs.format.z16, rs.format.bgr8]     # rs2_format is identifies how binary data is encoded within a frame
WIDTH       = 640              # Defines the number of columns for each frame or zero for auto resolve
HEIGHT      = 480              # Defines the number of lines for each frame or zero for auto resolve
FPS         = 30               # Defines the rate of frames per second
HFOV        = 87
DEPTH_RANGE = [0.1, 8.0]        # Replace with your sensor's specifics, in meter

# Obstacle distances in front of the sensor, starting from the left in increment degrees to the right
# See here: https://mavlink.io/en/messages/common.html#OBSTACLE_DISTANCE
MIN_DEPTH_CM = int(DEPTH_RANGE[0] * 100)  # In cm
MAX_DEPTH_CM = int(DEPTH_RANGE[1] * 100)  # In cm, being a little conservative
DISTANCES_ARRAY_LEN = 72
ANGLE_OFFSET = -(HFOV / 2)  # In the case of a forward facing camera with a horizontal wide view:
INCREMENT_F  = HFOV / DISTANCES_ARRAY_LEN
distances = np.ones((DISTANCES_ARRAY_LEN,), dtype=np.uint16) * (MAX_DEPTH_CM + 1)

# List of filters to be applied, in this order.
# https://github.com/IntelRealSense/librealsense/blob/master/doc/post-processing-filters.md
filters = [
    [True, "Decimation Filter",     rs.decimation_filter()],
    [True, "Threshold Filter",      rs.threshold_filter()],
    [True, "Depth to Disparity",    rs.disparity_transform(True)],
    [True, "Spatial Filter",        rs.spatial_filter()],
    [True, "Temporal Filter",       rs.temporal_filter()],
    [True, "Hole Filling Filter",   rs.hole_filling_filter()],
    [True, "Disparity to Depth",    rs.disparity_transform(False)]
]

######################################################
##                    Parameters                    ##
######################################################

# Default configurations for connection to the FCU
connection_string_default = '/dev/ttyUSB0'
connection_baudrate_default = 921600
connection_timeout_sec_default = 5

camera_orientation_default = 0

# Enable/disable each message/function individually
enable_msg_obstacle_distance = True
enable_msg_distance_sensor = False
obstacle_distance_msg_hz_default = 15

# Default global position for EKF home/ origin
enable_auto_set_ekf_home = False
home_lat = 151269321    # Somewhere random
home_lon = 16624301     # Somewhere random
home_alt = 163000       # Somewhere random

# TODO: Taken care of by ArduPilot, so can be removed (once the handling on AP side is confirmed stable)
# In NED frame, offset from the IMU or the center of gravity to the camera's origin point
body_offset_enabled = 0
body_offset_x = 0  # In meters (m)
body_offset_y = 0  # In meters (m)
body_offset_z = 0  # In meters (m)

# lock for thread synchronization
lock = threading.Lock()

debug_enable = True

#######################################
# Global variables
#######################################

# FCU connection variables
vehicle = None
is_vehicle_connected = False

# Camera-related variables
pipe = None
depth_scale = 0
if debug_enable is True:
    colorizer = rs.colorizer()

# Data variables
data = None
current_time_us = 0

#######################################
# Parsing user' inputs
#######################################

parser = argparse.ArgumentParser(description='Reboots vehicle')
parser.add_argument('--connect',
                    help="Vehicle connection target string. If not specified, a default string will be used.")
parser.add_argument('--baudrate', type=float,
                    help="Vehicle connection baudrate. If not specified, a default value will be used.")
parser.add_argument('--obstacle_distance_msg_hz', type=float,
                    help="Update frequency for OBSTACLE_DISTANCE message. If not specified, a default value will be used.")

args = parser.parse_args()

connection_string = args.connect
connection_baudrate = args.baudrate
obstacle_distance_msg_hz = args.obstacle_distance_msg_hz

# Using default values if no specified inputs
if not connection_string:
    connection_string = connection_string_default
    print("INFO: Using default connection_string", connection_string)
else:
    print("INFO: Using connection_string", connection_string)

if not connection_baudrate:
    connection_baudrate = connection_baudrate_default
    print("INFO: Using default connection_baudrate", connection_baudrate)
else:
    print("INFO: Using connection_baudrate", connection_baudrate)
    
if not obstacle_distance_msg_hz:
    obstacle_distance_msg_hz = obstacle_distance_msg_hz_default
    print("INFO: Using default obstacle_distance_msg_hz", obstacle_distance_msg_hz)
else:
    print("INFO: Using obstacle_distance_msg_hz", obstacle_distance_msg_hz)

# The list of filters to be applied on the depth image
for i in range(len(filters)):
    if filters[i][0] is True:
        print("INFO: Applying: ", filters[i][1])
    else:
        print("INFO: NOT applying: ", filters[i][1])

if not debug_enable:
    debug_enable = 0
else:
    debug_enable = 1
    np.set_printoptions(precision=4, suppress=True) # Format output on terminal 
    print("INFO: Debug messages enabled.")


#######################################
# Functions
#######################################

# https://mavlink.io/en/messages/common.html#OBSTACLE_DISTANCE
def send_obstacle_distance_message():
    global current_time_us, distances
    msg = vehicle.message_factory.obstacle_distance_encode(
        current_time_us,    # us Timestamp (UNIX time or time since system boot)
        0,                  # sensor_type, defined here: https://mavlink.io/en/messages/common.html#MAV_DISTANCE_SENSOR
        distances,          # distances,    uint16_t[72],   cm
        0,                  # increment,    uint8_t,        deg
        MIN_DEPTH_CM,	    # min_distance, uint16_t,       cm
        MAX_DEPTH_CM,       # max_distance, uint16_t,       cm
        INCREMENT_F,	    # increment_f,  float,          deg
        ANGLE_OFFSET,       # angle_offset, float,          deg
        12                  # MAV_FRAME, vehicle-front aligned: https://mavlink.io/en/messages/common.html#MAV_FRAME_BODY_FRD    
    )

    vehicle.send_mavlink(msg)
    vehicle.flush()

# https://mavlink.io/en/messages/common.html#DISTANCE_SENSOR
# To-do: Possible extension for individual object detection
def send_distance_sensor_message():
    global current_time, distances
    # Average out a portion of the centermost part
    curr_dist = int(np.mean(distances[33:38]))
    msg = vehicle.message_factory.distance_sensor_encode(
        0,              # us Timestamp (UNIX time or time since system boot) (ignored)
        MIN_DEPTH_CM,   # min_distance, uint16_t, cm
        MAX_DEPTH_CM,   # min_distance, uint16_t, cm
        curr_dist,      # current_distance,	uint16_t, cm	
        0,	            # type : 0 (ignored)
        0,              # id : 0 (ignored)
        0,              # forward
        0               # covariance : 0 (ignored)
    )

    vehicle.send_mavlink(msg)
    vehicle.flush()

def send_msg_to_gcs(text_to_be_sent):
    # MAV_SEVERITY: 0=EMERGENCY 1=ALERT 2=CRITICAL 3=ERROR, 4=WARNING, 5=NOTICE, 6=INFO, 7=DEBUG, 8=ENUM_END
    # Defined here: https://mavlink.io/en/messages/common.html#MAV_SEVERITY
    # MAV_SEVERITY = 3 will let the message be displayed on Mission Planner HUD, but 6 is ok for QGroundControl
    if is_vehicle_connected == True:
        text_msg = 'D4xx: ' + text_to_be_sent
        status_msg = vehicle.message_factory.statustext_encode(
            6,                      # MAV_SEVERITY
            text_msg.encode()	    # max size is char[50]       
        )
        vehicle.send_mavlink(status_msg)
        vehicle.flush()
        print("INFO: " + text_to_be_sent)
    else:
        print("INFO: Vehicle not connected. Cannot send text message to Ground Control Station (GCS)")

# Send a mavlink SET_GPS_GLOBAL_ORIGIN message (http://mavlink.org/messages/common#SET_GPS_GLOBAL_ORIGIN), which allows us to use local position information without a GPS.
def set_default_global_origin():
    if is_vehicle_connected == True:
        msg = vehicle.message_factory.set_gps_global_origin_encode(
            int(vehicle._master.source_system),
            home_lat, 
            home_lon,
            home_alt
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()

# Send a mavlink SET_HOME_POSITION message (http://mavlink.org/messages/common#SET_HOME_POSITION), which allows us to use local position information without a GPS.
def set_default_home_position():
    if is_vehicle_connected == True:
        x = 0
        y = 0
        z = 0
        q = [1, 0, 0, 0]   # w x y z

        approach_x = 0
        approach_y = 0
        approach_z = 1

        msg = vehicle.message_factory.set_home_position_encode(
            int(vehicle._master.source_system),
            home_lat, 
            home_lon,
            home_alt,
            x,
            y,
            z,
            q,
            approach_x,
            approach_y,
            approach_z
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()

# Request a timesync update from the flight controller, for future work.
# TODO: Inspect the usage of timesync_update 
def update_timesync(ts=0, tc=0):
    if ts == 0:
        ts = int(round(time.time() * 1000))
    msg = vehicle.message_factory.timesync_encode(
        tc,     # tc1
        ts      # ts1
    )
    vehicle.send_mavlink(msg)
    vehicle.flush()

# Listen to attitude data to acquire heading when compass data is enabled
def att_msg_callback(self, attr_name, value):
    global heading_north_yaw
    if heading_north_yaw is None:
        heading_north_yaw = value.yaw
        print("INFO: Received first ATTITUDE message with heading yaw", heading_north_yaw * 180 / m.pi, "degrees")
    else:
        heading_north_yaw = value.yaw
        print("INFO: Received ATTITUDE message with heading yaw", heading_north_yaw * 180 / m.pi, "degrees")

def vehicle_connect():
    global vehicle, is_vehicle_connected
    
    try:
        vehicle = connect(connection_string, wait_ready = True, baud = connection_baudrate, source_system = 1)
    except:
        print('Connection error! Retrying...')
        sleep(1)

    if vehicle == None:
        is_vehicle_connected = False
        return False
    else:
        is_vehicle_connected = True
        return True

def realsense_connect():
    global pipe, depth_scale
    # Declare RealSense pipe, encapsulating the actual device and sensors
    pipe = rs.pipeline()

    # Configure depth and color streams
    config = rs.config()
    config.enable_stream(STREAM_TYPE[0], WIDTH, HEIGHT, FORMAT[0], FPS)
    if debug_enable is True:
        config.enable_stream(STREAM_TYPE[1], WIDTH, HEIGHT, FORMAT[1], FPS)

    # Start streaming with requested config
    profile = pipe.start(config)

    # Getting the depth sensor's depth scale (see rs-align example for explanation)
    depth_sensor = profile.get_device().first_depth_sensor()
    depth_scale = depth_sensor.get_depth_scale()
    print("INFO: Depth scale is: ", depth_scale)

# Monitor user input from the terminal and perform action accordingly
def user_input_monitor():
    global scale_factor
    while True:
        if enable_auto_set_ekf_home:
            send_msg_to_gcs('Set EKF home with default GPS location')
            set_default_global_origin()
            set_default_home_position()
            time.sleep(1) # Wait a short while for FCU to start working

        # Add new action here according to the key pressed.
        # Enter: Set EKF home when user press enter
        try:
            c = input()
            if c == "":
                send_msg_to_gcs('Set EKF home with default GPS location')
                set_default_global_origin()
                set_default_home_position()
            else:
                print("Got keyboard input", c)
        except IOError: pass

@njit
def distances_from_depth_image(depth_mat, distances, min_depth_m, max_depth_m):
    depth_img_width  = depth_mat.shape[1]
    depth_img_height = depth_mat.shape[0]
    step = depth_img_width / DISTANCES_ARRAY_LEN
    for i in range(DISTANCES_ARRAY_LEN):
        dist_m = depth_mat[int(depth_img_height/2), int(i * step)] * depth_scale
        if dist_m < min_depth_m or dist_m > max_depth_m:
            distances[i] = max_depth_m * 100 + 1
        else:
            distances[i] = dist_m * 100

#######################################
# Main code starts here
#######################################

print("INFO: Connecting to vehicle.")
while (not vehicle_connect()):
    pass
print("INFO: Vehicle connected.")

send_msg_to_gcs('Connecting to camera...')
realsense_connect()
send_msg_to_gcs('Camera connected.')

# Send MAVlink messages in the background at pre-determined frequencies
sched = BackgroundScheduler()

# A separate thread to monitor user input
user_keyboard_input_thread = threading.Thread(target=user_input_monitor)
user_keyboard_input_thread.daemon = True
user_keyboard_input_thread.start()

sched.start()

if enable_msg_obstacle_distance:
    sched.add_job(send_obstacle_distance_message, 'interval', seconds = 1/obstacle_distance_msg_hz)
    send_msg_to_gcs('Sending obstacle distance messages to FCU')
elif enable_msg_distance_sensor:
    sched.add_job(send_distance_sensor_message, 'interval', seconds = 1/obstacle_distance_msg_hz)
    send_msg_to_gcs('Sending distance sensor messages to FCU')
else:
    send_msg_to_gcs('Nothing to do. Check params to enable something')
    pipe.stop()
    vehicle.close()
    print("INFO: Realsense pipe and vehicle object closed.")
    sys.exit()

print("INFO: Press Enter to set EKF home at default location")

# Begin of the main loop
last_time = time.time()
try:
    while True:
        # This call waits until a new coherent set of frames is available on a device
        # Calls to get_frame_data(...) and get_frame_timestamp(...) on a device will return stable values until wait_for_frames(...) is called
        frames = pipe.wait_for_frames()
        depth_frame = frames.get_depth_frame()

        if not depth_frame:
            continue

        # Store the timestamp for MAVLink messages
        current_time_us = int(round(time.time() * 1000000))

        # Apply the filters
        filtered_frame = depth_frame
        for i in range(len(filters)):
            if filters[i][0] is True:
                filtered_frame = filters[i][2].process(filtered_frame)

        # Extract depth in matrix form
        depth_data = filtered_frame.as_frame().get_data()
        depth_mat = np.asanyarray(depth_data)

        # Create obstacle distance data from depth image
        distances_from_depth_image(depth_mat, distances, DEPTH_RANGE[0], DEPTH_RANGE[1])

        if debug_enable:
            # Prepare the images
            display_name  = 'Input/output depth'
            input_image = np.asanyarray(colorizer.colorize(depth_frame).get_data())
            output_image = np.asanyarray(colorizer.colorize(filtered_frame).get_data())
            display_image = np.hstack((input_image, cv2.resize(output_image, (WIDTH, HEIGHT))))

            # Put the fps in the corner of the image
            processing_speed = 1 / (time.time() - last_time)
            text = ("%0.2f" % (processing_speed,)) + ' fps'
            textsize = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)[0]
            cv2.namedWindow(display_name, cv2.WINDOW_AUTOSIZE)
            cv2.putText(display_image, 
                        text,
                        org = (int((display_image.shape[1] - textsize[0]/2)), int((textsize[1])/2)),
                        fontFace = cv2.FONT_HERSHEY_SIMPLEX,
                        fontScale = 0.5,
                        thickness = 1,
                        color = (255, 255, 255))

            # Show the images
            cv2.imshow(display_name, display_image)
            cv2.waitKey(1)

            # Print all the distances in a line
            # print(*distances)
            
            last_time = time.time()

except KeyboardInterrupt:
    send_msg_to_gcs('Closing the script...')  

except Exception as e:
    print(e)
    pass

except:
    send_msg_to_gcs('ERROR: Depth camera disconnected')  

finally:
    pipe.stop()
    vehicle.close()
    print("INFO: Realsense pipe and vehicle object closed.")
    sys.exit()
