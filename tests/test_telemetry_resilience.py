"""Guard the Collector's bounded, independent persistent signal queues."""

from pathlib import Path
import unittest


CONFIG = Path(__file__).resolve().parents[1] / "docker" / "otelcol.yaml"
SIGNALS = ("traces", "metrics", "logs")


def exporter_block(config, signal):
    start = config.index(f"  otlp_http/openwoof_{signal}:\n")
    candidates = [
        config.find("\n  otlp_http/openwoof_", start + 1),
        config.find("\nservice:\n", start + 1),
    ]
    end = min(position for position in candidates if position >= 0)
    return config[start:end]


class TelemetryResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = CONFIG.read_text(encoding="utf-8")

    def test_each_signal_has_a_bounded_persistent_byte_queue(self):
        for signal in SIGNALS:
            with self.subTest(signal=signal):
                block = exporter_block(self.config, signal)
                self.assertIn("queue_size: 16777216", block)
                self.assertIn("sizer: bytes", block)
                self.assertIn("num_consumers: 1", block)
                self.assertIn("storage: file_storage/queue", block)
                self.assertIn("block_on_overflow: false", block)
                self.assertIn("wait_for_result: false", block)
                self.assertIn("max_elapsed_time: 0s", block)
                self.assertIn("max_interval: 30s", block)
                self.assertIn("max_size: 4194304", block)
                self.assertIn("min_size: 262144", block)
                self.assertIn("flush_timeout: 1s", block)

    def test_signal_pipelines_keep_privacy_processors_and_separate_storage(self):
        self.assertIn("directory: /data/telemetry/collector-queue\n", self.config)
        self.assertIn("max_size: 33554432", self.config)
        self.assertIn("directory: /data/telemetry/collector-checkpoints\n", self.config)
        self.assertIn("max_size: 4194304", self.config)
        for signal in SIGNALS:
            with self.subTest(signal=signal):
                self.assertIn(f"exporters: [otlp_http/openwoof_{signal}]", self.config)
        self.assertIn("processors: [memory_limiter, transform/privacy, batch]", self.config)
        self.assertIn("send_batch_size: 1", self.config)
        self.assertIn("send_batch_max_size: 1", self.config)


if __name__ == "__main__":
    unittest.main()
