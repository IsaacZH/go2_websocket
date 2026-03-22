python3 dds_to_foxglove.py --sdk-interface eth0 --ws-interface wlan0 --port 8765
python3 robot_control_ws_server.py --sdk-interface eth0 --host 0.0.0.0 --port 9001 --path /control
python demo_ws_control_client.py --host 192.168.1.105 --port 9001 --path /control
python keyboard_ws_control_client.py --host 172.25.14.2 --port 9001 --path /control