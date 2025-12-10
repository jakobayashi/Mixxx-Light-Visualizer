import serial, time
ser = serial.Serial("COM3", 115200, timeout=1)
ser.write(b"RGB 255 255 255\n"); ser.flush(); print(ser.readline())
time.sleep(5.0)
ser.write(b"RGB 0 0 0\n"); ser.flush(); print(ser.readline())
time.sleep(0.5)
ser.write(b"OFF\n"); ser.flush(); print(ser.readline())
ser.close()