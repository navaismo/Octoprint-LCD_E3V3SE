"""Offline protocol and exact host raster tests (no OctoPrint import needed)."""
import base64
import importlib
from pathlib import Path
import struct
import sys
import threading
import types
import unittest
import zlib

PACKAGE = "lcd_cleanup_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1]/"octoprint_LCD_E3V3SE")]
sys.modules[PACKAGE] = package
draw = importlib.import_module(PACKAGE+".drawing")
card = importlib.import_module(PACKAGE+".print_card")


def replay(operations):
    pixels, value = [0]* (240*320), 0
    for op in operations:
        if op[0] == 0:
            value = int.from_bytes(op[1:],"big")
        elif op[0] == 2:
            _,x,y,n = op[:4]
            for i in range(n):
                value = int.from_bytes(op[4+2*i:6+2*i],"big")
                pixels[y*240+x+i] = value
        else:
            _, x0,y0,x1,y1 = op
            for y in range(y0,y1+1):
                pixels[y*240+x0:y*240+x1+1] = [value]*(x1-x0+1)
    return pixels


class RasterTests(unittest.TestCase):
    def test_solid_alternating_vertical_background_and_unique(self):
        images = ([0]*9216, [0xFFFF]*9216, [i%2*0xF800 for i in range(9216)],
                  [y%2*0x07E0 for y in range(96) for x in range(96)],
                  list(range(1,9217)))
        for image in images:
            pixels = replay(card.card_operations(image))
            for y in range(96):
                self.assertEqual(pixels[(26+y)*240+12:(26+y)*240+108],image[y*96:y*96+96])
        self.assertEqual(len(list(card.merged_rectangles([1]*9216,96,96,12,25))),2)
        self.assertEqual(len(list(card.merged_rectangles([0]*9216,96,96,12,25))),0)

    def test_thumbnail_border_does_not_touch_title_progress_or_lower_rows(self):
        for image in (None, [0xFFFF]*9216):
            ops=card.card_operations(image)
            for op in ops:
                if op[0] == 1:
                    self.assertTrue(10<=op[1]<=op[3]<=109)
                    self.assertTrue(25<=op[2]<=op[4]<=122)
            pixels=replay(ops)
            for x in range(10,110):
                self.assertEqual(pixels[25*240+x],card.GREEN)
                self.assertEqual(pixels[122*240+x],card.GREEN)
            for y in range(25,123):
                self.assertEqual(pixels[y*240+10],card.GREEN)
                self.assertEqual(pixels[y*240+109],card.GREEN)
            self.assertTrue(all(pixels[y*240+x]==0 for y in range(25,123) for x in range(110,240)))
            self.assertEqual(pixels[24*240+10],0)
            self.assertEqual(pixels[123*240+10],0)

    def test_numbered_packets_and_crc(self):
        for data in draw.batches(card.card_operations(list(range(1,9217)))):
            command=draw.packet("D",0xFFFFFFFF,b"\xff\xfe"+data)
            line="N2147483647 "+command+"*255"
            self.assertLessEqual(len(line),95)
            raw=base64.b64decode(command[7:].replace("_","G"))
            self.assertEqual(zlib.crc32(b"D"+raw[:-4]),int.from_bytes(raw[-4:],"big"))
        with self.assertRaises(ValueError):
            draw.rect(0,23,20,30)
        with self.assertRaises(ValueError):
            list(card.merged_rectangles([1],96,96,12,25))

    def test_resize_and_transparency(self):
        try:
            from PIL import Image
            Image.new
        except (ImportError,AttributeError):
            self.skipTest("Run the Pillow test in the host Python environment")
        image=Image.new("RGBA",(192,96),(255,0,0,0))
        self.assertEqual(set(card.rgb565(image)),{0})
        image=Image.new("RGB",(192,192),(255,255,255))
        self.assertEqual(set(card.rgb565(image)),{65535})


class ClientTests(unittest.TestCase):
    def peer(self, drop=False):
        sent=[]
        def send(command):
            sent.append(command)
            if command=="M9001 Q":
                client.received("M9001 CAPS 2 42 240 320 00000000 0000")
                return
            kind=command[6]
            raw=base64.b64decode(command[7:].replace("_","G"))
            sid=int.from_bytes(raw[1:5],"big")
            seq=int.from_bytes(raw[5:7],"big") if kind in "DE" else 0
            if kind=="D" and drop and len([c for c in sent if c.startswith("M9001 D")])==1:
                return
            client.received("M9001 {} {:08X} {:04X}".format("DONE" if kind=="E" else "ACK",sid,seq+(kind=="D")))
        client=draw.DrawingClient(send)
        exchange=client._exchange
        client._exchange=lambda cmd,state,seq,timeout=0:exchange(cmd,state,seq,.01)
        client.connected()
        return client,sent

    def test_negotiation_one_credit_per_batch_and_done(self):
        client,sent=self.peer()
        client.render(card.card_operations())
        self.assertEqual(sent.count("M9001 Q"),1)
        self.assertFalse(any(c.startswith("M9001 U2") for c in sent))
        self.assertIsNone(client.sid)
        self.assertTrue(sent[-1].startswith("M9001 E"))
        self.assertFalse(any(len(c)>79 for c in sent))

    def test_dropped_credit_retries_exact_packet(self):
        client,sent=self.peer(drop=True)
        client.render([draw.color(1),draw.rect(12,25,12,25)])
        data=[c for c in sent if c.startswith("M9001 D")]
        self.assertEqual(data,[data[0],data[0]])

    def test_ok_stale_and_out_of_order_ack_do_not_grant_credit(self):
        client=draw.DrawingClient(lambda c:None)
        client.sid=123
        for line in ("ok","M9001 ACK 0000007C 0001","M9001 ACK rubbish 0001"):
            client.received(line)
        self.assertIsNone(client.response)
        client.received("M9001 ACK 0000007B 0000")
        with self.assertRaises(TimeoutError):
            client._exchange("M9001 test","ACK",1,.001)

    def test_abort_disconnect_and_single_session_lock(self):
        client,sent=self.peer()
        client.sid=123
        client.cancel(disconnected=True)
        self.assertEqual(sent,["M9001 Q"])
        self.assertIsNone(client.version)
        with self.assertRaises(RuntimeError):
            client.render([draw.color(0)])
        client.lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                client.render([draw.color(0)])
        finally:
            client.lock.release()

if __name__ == "__main__":
    unittest.main()
