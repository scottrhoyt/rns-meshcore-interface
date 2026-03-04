import time
import threading
import pytest
from rns_meshcore_interface.airtime import AirtimeController


class TestAirtimeController:
    def test_tx_delay_enforcement(self):
        ctrl = AirtimeController(tx_delay_ms=100)
        ctrl.record_tx()
        start = time.time()
        ctrl.wait_for_tx_slot()
        elapsed_ms = (time.time() - start) * 1000
        assert elapsed_ms >= 90  # allow small timing variance

    def test_no_delay_on_first_tx(self):
        ctrl = AirtimeController(tx_delay_ms=500)
        start = time.time()
        ctrl.wait_for_tx_slot()
        elapsed_ms = (time.time() - start) * 1000
        assert elapsed_ms < 50  # should not wait

    def test_duty_cycle_tracking(self):
        ctrl = AirtimeController(max_airtime_percent=50)
        assert ctrl.can_transmit()
        # Simulate heavy TX usage
        ctrl._tx_airtime_total = 10.0
        ctrl._tracking_start = time.time() - 10.0  # 10s elapsed, 10s TX = 100%
        assert not ctrl.can_transmit()

    def test_duty_cycle_percentage(self):
        ctrl = AirtimeController()
        ctrl._tx_airtime_total = 1.0
        ctrl._tracking_start = time.time() - 10.0
        pct = ctrl.current_duty_cycle()
        assert 9.0 < pct < 11.0  # ~10%

    def test_unlimited_duty_cycle(self):
        ctrl = AirtimeController(max_airtime_percent=0)
        ctrl._tx_airtime_total = 1000.0
        ctrl._tracking_start = time.time() - 1.0
        assert ctrl.can_transmit()  # 0 means unlimited


class TestAirtimeEstimation:
    def test_sf7_125khz(self):
        airtime = AirtimeController.estimate_airtime_seconds(50, sf=7, bw=125000, cr=1)
        assert 0.01 < airtime < 0.2  # reasonable range for SF7

    def test_sf12_longer_than_sf7(self):
        at7 = AirtimeController.estimate_airtime_seconds(50, sf=7, bw=125000, cr=1)
        at12 = AirtimeController.estimate_airtime_seconds(50, sf=12, bw=125000, cr=1)
        assert at12 > at7 * 10  # SF12 is dramatically slower

    def test_larger_payload_longer(self):
        at_small = AirtimeController.estimate_airtime_seconds(10, sf=7, bw=125000, cr=1)
        at_large = AirtimeController.estimate_airtime_seconds(200, sf=7, bw=125000, cr=1)
        assert at_large > at_small


class TestEffectiveBitrate:
    def test_basic_calculation(self):
        raw = 10000  # 10 kbps
        effective = AirtimeController.effective_bitrate(raw, tx_delay_ms=500)
        assert effective < raw
        assert effective > 0

    def test_zero_bitrate(self):
        assert AirtimeController.effective_bitrate(0, 500) == 0


class TestThreadSafety:
    def test_concurrent_record_tx(self):
        ctrl = AirtimeController(tx_delay_ms=0)
        errors = []

        def worker():
            try:
                for _ in range(100):
                    ctrl.record_tx(airtime_seconds=0.001)
                    ctrl.can_transmit()
                    ctrl.current_duty_cycle()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
