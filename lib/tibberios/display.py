# Initial version of this functionality modified from:
# https://github.com/waveshare/e-Paper/blob/master/RaspberryPi_JetsonNano/python/examples/epd_7in5_V2_test.py

import logging
import time

from PIL import Image

from .drivers.waveshare import epd7in5_V2


def update(image_path: str) -> None:
    try:
        print("Loading epd7in5_V2")
        epd = epd7in5_V2.EPD()
        print("Init and clear epd7in5_v2")
        epd.init()
        epd.Clear()

        print(f"Display image file: {image_path}")
        Himage = Image.open(image_path)
        epd.display(epd.getbuffer(Himage))
        time.sleep(2)

        print("Sleep epd7in5_v2...")
        epd.sleep()

    except IOError as e:
        print(f"IOError from epd7in5_v2: {e}")
        epd7in5_V2.epdconfig.module_exit()


def clear() -> None:
    epd = epd7in5_V2.EPD()
    print("Clear epd7in5_v2 display...")
    epd.init()
    epd.Clear()
