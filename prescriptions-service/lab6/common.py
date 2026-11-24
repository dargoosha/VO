from __future__ import annotations

import hashlib
import os
import platform
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sklearn.metrics import roc_auc_score
from torch import nn
from torch.utils.data import TensorDataset


DATASET_NAME = "octmnist"
DATASET_FILE = "octmnist.npz"
DATASET_URL = (
    "https://zenodo.org/records/10519652/files/octmnist.npz?download=1"
)
N_TRAIN = 7_000
N_CLASSES = 4
N_CHANNELS = 1
C1 = 64
C2 = 128
HIDDEN = 512
P_MAX = 4
GLOBAL_BATCH = 192
LOCAL_BATCH = GLOBAL_BATCH // P_MAX
CHECKPOINT_INTERVAL = 20
FAILURE_STEP = 50
BASE_LR = 0.02
EPOCHS = 3
SEED = 0


@dataclass(frozen=True)
class Variant:
    number: int = 6
    dataset_mark: str = "Д6"
    dataset_name: str = DATASET_NAME
    width_mark: str = "Ш3"
    classes: int = N_CLASSES
    channels: int = N_CHANNELS
    c1: int = C1
    c2: int = C2
    hidden: int = HIDDEN
    p_max: int = P_MAX
    global_batch: int = GLOBAL_BATCH
    checkpoint_interval: int = CHECKPOINT_INTERVAL
    failure_step: int = FAILURE_STEP


def configure_runtime(threads: int, seed: int = SEED) -> None:
    if threads <= 0:
        raise ValueError("threads must be positive")
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(threads))
    os.environ.setdefault("GLOO_SOCKET_IFNAME", "lo")
    torch.set_num_threads(threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # The value may already be fixed after the first parallel operation.
        pass
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)


class Net(nn.Module):
    def __init__(
        self,
        channels: int = N_CHANNELS,
        c1: int = C1,
        c2: int = C2,
        hidden: int = HIDDEN,
        classes: int = N_CLASSES,
    ) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(channels, c1, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(c1, c2, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
        )
        self.head = nn.Sequential(
            nn.Flatten(),
            nn.Linear(7 * 7 * c2, hidden),
            nn.ReLU(),
            nn.Linear(hidden, classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.features(x))


def n_params_formula(
    channels: int = N_CHANNELS,
    c1: int = C1,
    c2: int = C2,
    hidden: int = HIDDEN,
    classes: int = N_CLASSES,
) -> int:
    return (
        9 * channels * c1
        + c1
        + 9 * c1 * c2
        + c2
        + 49 * c2 * hidden
        + hidden
        + hidden * classes
        + classes
    )


def model_metadata() -> dict[str, Any]:
    model = Net()
    actual = sum(parameter.numel() for parameter in model.parameters())
    expected = n_params_formula()
    if actual != expected:
        raise AssertionError(f"parameter count mismatch: {actual} != {expected}")
    gradient_bytes = 4 * actual
    return {
        "parameter_tensors": sum(1 for _ in model.parameters()),
        "n_params": actual,
        "gradient_bytes": gradient_bytes,
        "gradient_mib": gradient_bytes / (1024**2),
    }


def load_split(path: Path, split: str, n: int | None = None) -> TensorDataset:
    if split not in {"train", "test"}:
        raise ValueError("split must be 'train' or 'test'")
    with np.load(path) as data:
        images = data[f"{split}_images"][:n]
        labels = data[f"{split}_labels"][:n].reshape(-1)
    if images.ndim == 3:
        images = images[:, None, :, :]
    elif images.ndim == 4:
        images = images.transpose(0, 3, 1, 2)
    else:
        raise ValueError(f"unsupported image shape: {images.shape}")
    images_tensor = torch.from_numpy(np.ascontiguousarray(images)).float()
    images_tensor = (images_tensor / 255.0 - 0.5) / 0.5
    labels_tensor = torch.from_numpy(labels.astype(np.int64, copy=False))
    return TensorDataset(images_tensor, labels_tensor)


@torch.inference_mode()
def evaluate(model: nn.Module, dataset: TensorDataset, batch_size: int = 256) -> dict[str, float]:
    model.eval()
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=0,
    )
    labels_parts: list[np.ndarray] = []
    probability_parts: list[np.ndarray] = []
    for images, labels in loader:
        probabilities = torch.softmax(model(images), dim=1)
        labels_parts.append(labels.numpy())
        probability_parts.append(probabilities.numpy())
    labels_np = np.concatenate(labels_parts)
    probabilities_np = np.concatenate(probability_parts)
    predictions = probabilities_np.argmax(axis=1)
    return {
        "acc": float(np.mean(predictions == labels_np)),
        "auc": float(
            roc_auc_score(
                labels_np,
                probabilities_np,
                multi_class="ovr",
                average="macro",
            )
        ),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_metadata() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "pytorch": torch.__version__,
        "numpy": np.__version__,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus_visible": os.cpu_count(),
        "torch_threads": torch.get_num_threads(),
        "torch_interop_threads": torch.get_num_interop_threads(),
        "gloo_socket_ifname": os.environ.get("GLOO_SOCKET_IFNAME"),
    }


def scaling_metrics(throughput: float, baseline: float, processes: int) -> dict[str, float | None]:
    speedup = throughput / baseline
    efficiency = speedup / processes
    karp_flatt = None
    if processes > 1:
        karp_flatt = (1.0 / speedup - 1.0 / processes) / (1.0 - 1.0 / processes)
    return {"speedup": speedup, "efficiency": efficiency, "karp_flatt": karp_flatt}

