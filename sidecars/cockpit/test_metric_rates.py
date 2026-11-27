import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metric_rates  # noqa: E402


class MetricRatesTests(unittest.TestCase):
    def test_normal_stream_call_uses_full_output(self):
        call = {
            "is_streamed": True,
            "out": 600,
            "think": 350,
            "resp": 250,
            "ttft": 5.0,
            "stream": 3.0,
            "start_ts": 10.0,
            "end_ts": 18.0,
        }
        perf = metric_rates.db_perf([call])
        self.assertAlmostEqual(perf["tps"], 200.0, places=3)
        self.assertEqual(perf["tps_calls"], 1)

    def test_buffered_thinking_in_ttft_uses_visible_resp_instead_of_inflated_out(self):
        # Real-world case: 268 think tokens buffered in 7.33s TTFT, 52 resp tokens streamed in 0.209s.
        call = {
            "is_streamed": True,
            "out": 320,
            "think": 268,
            "resp": 52,
            "ttft": 7.33,
            "stream": 0.209,
            "start_ts": 100.0,
            "end_ts": 107.6,
        }
        perf = metric_rates.db_perf([call])
        self.assertIsNotNone(perf["tps"])
        self.assertAlmostEqual(perf["tps"], 52.0 / 0.209, places=2)
        self.assertEqual(perf["tps_calls"], 1)

    def test_non_streaming_burst_sample_is_excluded_from_decode_tps(self):
        burst = {
            "is_streamed": True,
            "out": 300,
            "think": 300,
            "resp": 0,
            "ttft": 6.0,
            "stream": 0.02,
            "start_ts": 10.0,
            "end_ts": 16.02,
        }
        perf = metric_rates.db_perf([burst])
        self.assertIsNone(perf["tps"])
        self.assertEqual(perf["tps_calls"], 0)
        self.assertIsNotNone(perf["request_speed"]["tps"])


if __name__ == "__main__":
    unittest.main()
