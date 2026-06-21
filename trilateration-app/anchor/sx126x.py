# This file is used for LoRa and Raspberry pi4B related issues
#
# MODIFIED VERSION — fixes the RSSI capture bug in receive().
#
# The original Waveshare driver enabled RSSI mode (rssi_temp = 0x80) but
# never actually read the trailing RSSI byte that the SX126x module appends
# to every received packet. receive() used to slice it off and throw it away:
#     data_slice = r_buff[3:-1]   # <-- last byte (RSSI) discarded, never used
#
# This version reads that last byte and converts it to a real dBm value,
# and returns (message, rssi) instead of just printing to the terminal.

import RPi.GPIO as GPIO
import serial
import time

class sx126x:

    M0 = 22
    M1 = 27
    # if the header is 0xC0, then the LoRa register settings dont lost when it poweroff, and 0xC2 will be lost.
    # cfg_reg = [0xC0,0x00,0x09,0x00,0x00,0x00,0x62,0x00,0x17,0x43,0x00,0x00]
    cfg_reg = [0xC2,0x00,0x09,0x00,0x00,0x00,0x62,0x00,0x12,0x43,0x00,0x00]
    get_reg = bytes(12)
    rssi = False
    addr = 65535
    serial_n = ""
    addr_temp = 0

    #
    # start frequence of two lora module
    #
    # E22-400T22S           E22-900T22S
    # 410~493MHz      or    850~930MHz
    start_freq = 850

    #
    # offset between start and end frequence of two lora module
    #
    # E22-400T22S           E22-900T22S
    # 410~493MHz      or    850~930MHz
    offset_freq = 18

    SX126X_UART_BAUDRATE_1200 = 0x00
    SX126X_UART_BAUDRATE_2400 = 0x20
    SX126X_UART_BAUDRATE_4800 = 0x40
    SX126X_UART_BAUDRATE_9600 = 0x60
    SX126X_UART_BAUDRATE_19200 = 0x80
    SX126X_UART_BAUDRATE_38400 = 0xA0
    SX126X_UART_BAUDRATE_57600 = 0xC0
    SX126X_UART_BAUDRATE_115200 = 0xE0

    SX126X_PACKAGE_SIZE_240_BYTE = 0x00
    SX126X_PACKAGE_SIZE_128_BYTE = 0x40
    SX126X_PACKAGE_SIZE_64_BYTE = 0x80
    SX126X_PACKAGE_SIZE_32_BYTE = 0xC0

    SX126X_Power_22dBm = 0x00
    SX126X_Power_17dBm = 0x01
    SX126X_Power_13dBm = 0x02
    SX126X_Power_10dBm = 0x03

    lora_air_speed_dic = {
        1200:0x01,
        2400:0x02,
        4800:0x03,
        9600:0x04,
        19200:0x05,
        38400:0x06,
        62500:0x07
    }

    lora_power_dic = {
        22:0x00,
        17:0x01,
        13:0x02,
        10:0x03
    }

    lora_buffer_size_dic = {
        240:SX126X_PACKAGE_SIZE_240_BYTE,
        128:SX126X_PACKAGE_SIZE_128_BYTE,
        64:SX126X_PACKAGE_SIZE_64_BYTE,
        32:SX126X_PACKAGE_SIZE_32_BYTE
    }

    def __init__(self,serial_num,freq,addr,power,rssi,air_speed=2400,\
                 net_id=0,buffer_size = 240,crypt=0,\
                 relay=False,lbt=False,wor=False):
        self.rssi = rssi
        self.addr = addr
        self.freq = freq
        self.serial_n = serial_num
        self.power = power
        # Initial the GPIO for M0 and M1 Pin
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.M0,GPIO.OUT)
        GPIO.setup(self.M1,GPIO.OUT)
        GPIO.output(self.M0,GPIO.LOW)
        GPIO.output(self.M1,GPIO.HIGH)

        # The hardware UART of Pi3B+,Pi4B is /dev/ttyS0
        self.ser = serial.Serial(serial_num,9600)
        self.ser.flushInput()
        self.set(freq,addr,power,rssi,air_speed,net_id,buffer_size,crypt,relay,lbt,wor)

    def set(self,freq,addr,power,rssi,air_speed=2400,\
            net_id=0,buffer_size = 240,crypt=0,\
            relay=False,lbt=False,wor=False):
        self.send_to = addr
        self.addr = addr
        # frequence has two segments, 410-493 or 850-930
        if freq > 850:
            freq_temp = freq - 850
            self.start_freq = 850
            self.offset_freq = freq_temp
        elif freq > 410:
            freq_temp = freq - 410
            self.start_freq = 410
            self.offset_freq = freq_temp

        self.freq = freq
        self.power = power
        self.rssi = rssi
        # GPIO.output(self.M0,GPIO.LOW)
        # GPIO.output(self.M1,GPIO.HIGH)
        time.sleep(0.1)

        low_addr = addr & 0xff
        high_addr = addr >> 8 & 0xff
        net_id_temp = net_id
        if freq > 850:
            freq_temp = freq - 850
        elif freq > 410:
            freq_temp = freq - 410
        air_speed_temp = self.lora_air_speed_dic.get(air_speed,None)
        buffer_size_temp = self.lora_buffer_size_dic.get(buffer_size,None)
        power_temp = self.lora_power_dic.get(power,None)

        if rssi:
            # enable print rssi value
            rssi_temp = 0x80
        else:
            # disable print rssi value
            rssi_temp = 0x00

        # get crypt
        l_crypt = crypt & 0xff
        h_crypt = crypt >> 8 & 0xff
        if relay==False:
            self.cfg_reg[3] = high_addr
            self.cfg_reg[4] = low_addr
            self.cfg_reg[5] = net_id_temp
            if air_speed_temp is not None:
                self.cfg_reg[6] = 0x00 + air_speed_temp
            else:
                self.cfg_reg[6] = 0x62
            if buffer_size_temp is not None and power_temp is not None:
                self.cfg_reg[7] = buffer_size_temp + power_temp + 0x00
            else:
                self.cfg_reg[7] = 0x00
            self.cfg_reg[8] = freq_temp
            # it will enable to read noise rssi value when add 0x20 as follow
            # self.cfg_reg[9] = 0x03 + rssi_temp + 0x20
            # it will output a packet rssi value following received message
            # when enable eighth bit with 06H register(rssi_temp = 0x80)
            self.cfg_reg[9] = 0x43 + rssi_temp
            self.cfg_reg[10] = h_crypt
            self.cfg_reg[11] = l_crypt
        else:
            self.cfg_reg[3] = 0x01
            self.cfg_reg[4] = 0x02
            self.cfg_reg[5] = net_id_temp
            if air_speed_temp is not None:
                self.cfg_reg[6] = 0x03 + air_speed_temp
            else:
                self.cfg_reg[6] = 0x65
            if buffer_size_temp is not None and power_temp is not None:
                self.cfg_reg[7] = buffer_size_temp + power_temp + 0x00
            else:
                self.cfg_reg[7] = 0x00
            self.cfg_reg[8] = freq_temp
            self.cfg_reg[9] = 0x03 + rssi_temp
            self.cfg_reg[10] = h_crypt
            self.cfg_reg[11] = l_crypt

        GPIO.output(self.M1,GPIO.HIGH)
        GPIO.output(self.M0,GPIO.LOW)
        time.sleep(0.1)
        self.ser.flushInput()

        for i in range(2):
            self.ser.write(bytes(self.cfg_reg))
            r_buff = 0
            time.sleep(0.2)
            if self.ser.inWaiting() > 0:
                time.sleep(0.1)
                r_buff = self.ser.read(self.ser.inWaiting())
                if r_buff[0] == 0xC1:
                    pass
                else:
                    time.sleep(0.2)
                    if self.ser.inWaiting() > 0:
                        self.ser.read(self.ser.inWaiting())
            else:
                pass

        GPIO.output(self.M1,GPIO.LOW)
        time.sleep(0.1)

    def send(self,data):
        GPIO.output(self.M1,GPIO.LOW)
        GPIO.output(self.M0,GPIO.LOW)
        time.sleep(0.1)

        self.ser.write(data)
        time.sleep(0.1)

    def receive(self):
        """
        FIXED: now actually reads the trailing RSSI byte that the SX126x
        appends to every received packet (when rssi=True was set at init),
        instead of slicing it off and discarding it.

        Returns:
            (message: str, rssi_dbm: int or None) on a successful receive
            (None, None) if there was nothing to read
        """
        if self.ser.inWaiting() > 0:
            time.sleep(0.5)
            r_buff = self.ser.read(self.ser.inWaiting())

            if len(r_buff) < 4:
                # too short to contain header + message + rssi byte
                return None, None

            if self.rssi:
                # last byte is the packet RSSI, formula per Waveshare docs:
                # rssi_dbm = -(256 - byte_value)
                rssi_byte = r_buff[-1]
                rssi_dbm = -(256 - rssi_byte)
                data_slice = r_buff[3:-1]
            else:
                rssi_dbm = None
                data_slice = r_buff[3:]

            try:
                message = data_slice.decode(errors="ignore")
            except Exception:
                message = ""

            return message, rssi_dbm

        return None, None

    def get_channel_rssi(self):
        GPIO.output(self.M1,GPIO.LOW)
        GPIO.output(self.M0,GPIO.LOW)
        time.sleep(0.1)
        self.ser.flushInput()
        self.ser.write(bytes([0xC0,0xC1,0xC2,0xC3,0x00,0x02]))
        time.sleep(0.5)
        re_temp = bytes(5)
        if self.ser.inWaiting() > 0:
            time.sleep(0.1)
            re_temp = self.ser.read(self.ser.inWaiting())
        if re_temp[0] == 0xC1 and re_temp[1] == 0x00 and re_temp[2] == 0x02:
            noise_rssi = -(256 - re_temp[3])
            return noise_rssi
        else:
            return None

### END OF FILE ###
