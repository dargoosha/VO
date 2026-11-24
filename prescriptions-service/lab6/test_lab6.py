from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch
from torch import nn

from lab6.common import (
    C1,
    C2,
    GLOBAL_BATCH,
    HIDDEN,
    LOCAL_BATCH,
    N_CHANNELS,
    N_CLASSES,
    Net,
    load_split,
    model_metadata,
    n_params_formula,
    scaling_metrics,
)


class ModelTests(unittest.TestCase):
    def test_variant_parameter_formula(self) -> None:
        self.assertEqual(
            n_params_formula(N_CHANNELS, C1, C2, HIDDEN, N_CLASSES),
            3_288_324,
        )
        metadata = model_metadata()
        self.assertEqual(metadata["n_params"], 3_288_324)
        self.assertEqual(metadata["gradient_bytes"], 13_153_296)
        self.assertEqual(metadata["parameter_tensors"], 8)

    def test_model_shape_and_no_batch_norm(self) -> None:
        model = Net()
        output = model(torch.zeros(5, 1, 28, 28))
        self.assertEqual(tuple(output.shape), (5, 4))
        self.assertFalse(any(isinstance(layer, nn.modules.batchnorm._BatchNorm) for layer in model.modules()))

    def test_variant_batches_are_divisible(self) -> None:
        self.assertEqual(GLOBAL_BATCH, 192)
        self.assertEqual(LOCAL_BATCH, 48)
        for processes in range(1, 5):
            self.assertEqual(GLOBAL_BATCH % processes, 0)


class DataTests(unittest.TestCase):
    def test_load_grayscale_split(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tiny.npz"
            images = np.arange(3 * 28 * 28, dtype=np.uint8).reshape(3, 28, 28)
            labels = np.array([[0], [1], [2]], dtype=np.uint8)
            np.savez(path, train_images=images, train_labels=labels)
            dataset = load_split(path, "train")
            x, y = dataset.tensors
            self.assertEqual(tuple(x.shape), (3, 1, 28, 28))
            self.assertEqual(tuple(y.shape), (3,))
            self.assertEqual(x.dtype, torch.float32)
            self.assertEqual(y.dtype, torch.int64)
            self.assertGreaterEqual(float(x.min()), -1.0)
            self.assertLessEqual(float(x.max()), 1.0)


class AnalysisTests(unittest.TestCase):
    def test_scaling_formulas(self) -> None:
        metrics = scaling_metrics(180.0, 100.0, 2)
        self.assertAlmostEqual(metrics["speedup"], 1.8)
        self.assertAlmostEqual(metrics["efficiency"], 0.9)
        self.assertAlmostEqual(metrics["karp_flatt"], 1.0 / 9.0)
        baseline = scaling_metrics(100.0, 100.0, 1)
        self.assertEqual(baseline["speedup"], 1.0)
        self.assertEqual(baseline["efficiency"], 1.0)
        self.assertIsNone(baseline["karp_flatt"])


if __name__ == "__main__":
    unittest.main()
