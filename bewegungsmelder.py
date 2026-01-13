from gpiozero import MotionSensor,LED
from time import sleep
from datetime import datetime

pir = MotionSensor(18)
led_blu = LED(17)
led_blu.off
led_yel = LED(23)
led_yel.on
LOGFILE = "/home/pipapo/motion.log"

while True:
    led_yel.off()
    
    pir.wait_for_motion()
    with open(LOGFILE, "a", encoding = "utf-8") as f:
        f.write(f"{datetime.now()}\n")
    print("Motion detected!")
    
    led_blu.on()
    led_yel.off()
    sleep(0.2)
    
    led_blu.off()
    led_yel.on()
    sleep(0.2)
    
    led_yel.off()
    sleep(4)
