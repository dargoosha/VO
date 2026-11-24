from __future__ import annotations

import argparse
import copy
import json
import math
import os
import statistics
import sys
import time
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

import torch
import torch.distributed as dist
from torch import nn
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader, DistributedSampler, TensorDataset

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab6.common import (  # noqa: E402
    BASE_LR,
    CHECKPOINT_INTERVAL,
    EPOCHS,
    FAILURE_STEP,
    GLOBAL_BATCH,
    N_TRAIN,
    Net,
    configure_runtime,
    evaluate,
    load_split,
    model_metadata,
    runtime_metadata,
)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def distributed_mode(mode: str) -> bool:
    return mode in {"manual", "ddp", "ddp-no-sync"}


def init_distributed(mode: str) -> tuple[int, int]:
    if not distributed_mode(mode):
        return 0, 1
    timeout = timedelta(seconds=120)
    rendezvous = dist.rendezvous("env://", timeout=timeout)
    store, rank, world_size = next(rendezvous)
    restart_count = int(os.environ.get("TORCHELASTIC_RESTART_COUNT", "0"))
    generation_store = dist.PrefixStore(
        f"lab6_restart_{restart_count}",
        store,
    )
    dist.init_process_group(
        "gloo",
        store=generation_store,
        rank=rank,
        world_size=world_size,
        timeout=timeout,
    )
    return dist.get_rank(), dist.get_world_size()


def cleanup_distributed(mode: str) -> None:
    if distributed_mode(mode) and dist.is_initialized():
        dist.destroy_process_group()


def base_model(model: nn.Module) -> Net:
    if isinstance(model, DistributedDataParallel):
        return model.module  # type: ignore[return-value]
    return model  # type: ignore[return-value]


def make_model(mode: str) -> nn.Module:
    model = Net()
    if mode == "manual":
        for parameter in model.parameters():
            dist.broadcast(parameter.data, src=0)
    elif mode in {"ddp", "ddp-no-sync"}:
        model = DistributedDataParallel(model, bucket_cap_mb=25)
    return model


def make_loader(
    dataset: TensorDataset,
    *,
    local_batch: int,
    rank: int,
    world_size: int,
    distributed: bool,
) -> tuple[DataLoader, DistributedSampler | None]:
    sampler: DistributedSampler | None = None
    if distributed:
        sampler = DistributedSampler(
            dataset,
            num_replicas=world_size,
            rank=rank,
            shuffle=True,
            seed=0,
            drop_last=False,
        )
    loader = DataLoader(
        dataset,
        batch_size=local_batch,
        sampler=sampler,
        shuffle=not distributed,
        drop_last=True,
        num_workers=0,
    )
    return loader, sampler


def batch_stream(
    loader: DataLoader,
    sampler: DistributedSampler | None,
) -> Iterator[tuple[torch.Tensor, torch.Tensor]]:
    epoch = 0
    while True:
        if sampler is not None:
            sampler.set_epoch(epoch)
        for batch in loader:
            yield batch
        epoch += 1


def manual_average_gradients(model: nn.Module, world_size: int) -> float:
    started = time.perf_counter()
    for parameter in model.parameters():
        if parameter.grad is None:
            raise AssertionError("missing gradient")
        dist.all_reduce(parameter.grad, op=dist.ReduceOp.SUM)
        parameter.grad /= world_size
    return time.perf_counter() - started


def gradient_equivalence(
    model: nn.Module,
    dataset: TensorDataset,
    global_batch: int,
    rank: int,
    world_size: int,
) -> dict[str, Any] | None:
    if global_batch % world_size != 0:
        raise ValueError("global batch must be divisible by world size")
    state = copy.deepcopy(base_model(model).state_dict())
    images, labels = dataset.tensors
    full_images = images[:global_batch]
    full_labels = labels[:global_batch]
    local_indices = torch.arange(rank, global_batch, world_size)

    base_model(model).zero_grad(set_to_none=True)
    local_logits = base_model(model)(full_images[local_indices])
    nn.functional.cross_entropy(local_logits, full_labels[local_indices]).backward()
    manual_average_gradients(base_model(model), world_size)

    result: dict[str, Any] | None = None
    if rank == 0:
        reference = Net()
        reference.load_state_dict(state)
        reference.zero_grad(set_to_none=True)
        reference_logits = reference(full_images)
        nn.functional.cross_entropy(reference_logits, full_labels).backward()
        ratios: list[float] = []
        absolute_differences: list[float] = []
        for reference_parameter, parallel_parameter in zip(
            reference.parameters(),
            base_model(model).parameters(),
            strict=True,
        ):
            if reference_parameter.grad is None or parallel_parameter.grad is None:
                raise AssertionError("missing gradient during comparison")
            max_reference = float(reference_parameter.grad.abs().max().item())
            max_difference = float(
                (reference_parameter.grad - parallel_parameter.grad).abs().max().item()
            )
            absolute_differences.append(max_difference)
            ratios.append(
                max_difference / max_reference if max_reference > 0.0 else max_difference
            )
        result = {
            "max_relative_gradient_difference": max(ratios),
            "max_absolute_gradient_difference": max(absolute_differences),
            "per_tensor_relative_differences": ratios,
            "local_indices_rank0": local_indices.tolist(),
        }
    base_model(model).load_state_dict(state)
    base_model(model).zero_grad(set_to_none=True)
    dist.barrier()
    return result


def one_speed_step(
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    images: torch.Tensor,
    labels: torch.Tensor,
    mode: str,
    world_size: int,
) -> tuple[float, float, float]:
    optimizer.zero_grad(set_to_none=True)
    started = time.perf_counter()
    sync_s = 0.0
    context = model.no_sync() if mode == "ddp-no-sync" else nullcontext()
    with context:
        logits = model(images)
        loss = nn.functional.cross_entropy(logits, labels)
        loss.backward()
    if mode == "manual":
        sync_s = manual_average_gradients(model, world_size)
    optimizer.step()
    return time.perf_counter() - started, sync_s, float(loss.detach().item())


def weight_sum_range(model: nn.Module, rank: int, world_size: int, mode: str) -> float:
    local_sum = torch.tensor(
        [
            sum(
                float(parameter.detach().double().sum().item())
                for parameter in base_model(model).parameters()
            )
        ],
        dtype=torch.float64,
    )
    if not distributed_mode(mode):
        return 0.0
    gathered = [torch.zeros_like(local_sum) for _ in range(world_size)]
    dist.all_gather(gathered, local_sum)
    values = [float(item.item()) for item in gathered]
    if rank == 0:
        return max(values) - min(values)
    return math.nan


def run_speed(args: argparse.Namespace, rank: int, world_size: int) -> None:
    if args.global_batch % world_size != 0:
        raise ValueError("global batch must be divisible by world size")
    local_batch = args.global_batch // world_size
    dataset = load_split(args.data, "train", n=N_TRAIN)
    model = make_model(args.mode)
    initial_state = copy.deepcopy(base_model(model).state_dict())

    equivalence = None
    if args.mode == "manual":
        equivalence = gradient_equivalence(
            model,
            dataset,
            args.global_batch,
            rank,
            world_size,
        )

    loader, sampler = make_loader(
        dataset,
        local_batch=local_batch,
        rank=rank,
        world_size=world_size,
        distributed=distributed_mode(args.mode),
    )
    stream = batch_stream(loader, sampler)
    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)

    if distributed_mode(args.mode):
        dist.barrier()
    for _ in range(args.warmup_steps):
        images, labels = next(stream)
        one_speed_step(model, optimizer, images, labels, args.mode, world_size)

    base_model(model).load_state_dict(initial_state)
    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)
    stream = batch_stream(loader, sampler)
    if distributed_mode(args.mode):
        dist.barrier()

    step_times: list[float] = []
    sync_times: list[float] = []
    losses: list[float] = []
    measurement_started = time.perf_counter()
    for _ in range(args.measure_steps):
        images, labels = next(stream)
        step_s, sync_s, loss = one_speed_step(
            model,
            optimizer,
            images,
            labels,
            args.mode,
            world_size,
        )
        if rank == 0:
            step_times.append(step_s)
            sync_times.append(sync_s)
            losses.append(loss)
    measured_duration_s = time.perf_counter() - measurement_started
    sum_range = weight_sum_range(model, rank, world_size, args.mode)

    if rank == 0:
        median_step_s = statistics.median(step_times)
        result = {
            "kind": "speed",
            "mode": args.mode,
            "world_size": world_size,
            "global_batch": args.global_batch,
            "local_batch": local_batch,
            "learning_rate": args.lr,
            "warmup_steps": args.warmup_steps,
            "measure_steps": args.measure_steps,
            "median_step_s": median_step_s,
            "median_step_ms": median_step_s * 1000.0,
            "measured_duration_s": measured_duration_s,
            "throughput_examples_s": (
                args.measure_steps * args.global_batch / measured_duration_s
            ),
            "median_sync_s": statistics.median(sync_times),
            "median_sync_ms": statistics.median(sync_times) * 1000.0,
            "weight_sum_range_after_50_steps": sum_range,
            "loss_first": losses[0],
            "loss_last": losses[-1],
            "gradient_equivalence": equivalence,
            "model": model_metadata(),
            "runtime": runtime_metadata(),
        }
        atomic_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)


def global_mean_loss(loss: torch.Tensor, world_size: int, mode: str) -> float:
    detached = loss.detach().double()
    if distributed_mode(mode):
        dist.all_reduce(detached, op=dist.ReduceOp.SUM)
        detached /= world_size
    return float(detached.item())


def train_epochs(
    *,
    args: argparse.Namespace,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    train_dataset: TensorDataset,
    rank: int,
    world_size: int,
) -> tuple[list[dict[str, Any]], int]:
    local_batch = args.global_batch // world_size
    loader, sampler = make_loader(
        train_dataset,
        local_batch=local_batch,
        rank=rank,
        world_size=world_size,
        distributed=distributed_mode(args.mode),
    )
    losses: list[dict[str, Any]] = []
    step = 0
    model.train()
    for epoch in range(args.epochs):
        if sampler is not None:
            sampler.set_epoch(epoch)
        for step_in_epoch, (images, labels) in enumerate(loader, start=1):
            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = nn.functional.cross_entropy(logits, labels)
            loss.backward()
            optimizer.step()
            step += 1
            mean_loss = global_mean_loss(loss, world_size, args.mode)
            if rank == 0:
                losses.append(
                    {
                        "step": step,
                        "epoch": epoch,
                        "step_in_epoch": step_in_epoch,
                        "loss": mean_loss,
                    }
                )
    return losses, step


def run_train(args: argparse.Namespace, rank: int, world_size: int) -> None:
    if args.global_batch % world_size != 0:
        raise ValueError("global batch must be divisible by world size")
    train_dataset = load_split(args.data, "train", n=N_TRAIN)
    test_dataset = load_split(args.data, "test")
    model = make_model(args.mode)
    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)
    started = time.perf_counter()
    losses, steps = train_epochs(
        args=args,
        model=model,
        optimizer=optimizer,
        train_dataset=train_dataset,
        rank=rank,
        world_size=world_size,
    )
    training_duration_s = time.perf_counter() - started

    if rank == 0:
        quality = evaluate(base_model(model), test_dataset)
        last_epoch = args.epochs - 1
        max_last_loss = max(
            item["loss"] for item in losses if item["epoch"] == last_epoch
        )
        result = {
            "kind": "quality",
            "mode": args.mode,
            "world_size": world_size,
            "global_batch": args.global_batch,
            "local_batch": args.global_batch // world_size,
            "learning_rate": args.lr,
            "epochs": args.epochs,
            "optimizer_steps": steps,
            "training_duration_s": training_duration_s,
            "acc": quality["acc"],
            "auc": quality["auc"],
            "max_loss_last_epoch": max_last_loss,
            "losses": losses,
            "model": model_metadata(),
            "runtime": runtime_metadata(),
        }
        atomic_json(args.output, result)
        print(json.dumps({k: v for k, v in result.items() if k != "losses"}), flush=True)
    if distributed_mode(args.mode):
        dist.barrier()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(
    *,
    checkpoint: Path,
    checkpoint_event: Path,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    epoch: int,
    step_in_epoch: int,
    step: int,
    losses: list[dict[str, Any]],
) -> None:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint.with_suffix(".tmp")
    torch.save(
        {
            "model": base_model(model).state_dict(),
            "opt": optimizer.state_dict(),
            "epoch": epoch,
            "step_in_epoch": step_in_epoch,
            "step": step,
            "losses": losses,
        },
        temporary,
    )
    os.replace(temporary, checkpoint)
    atomic_json(
        checkpoint_event,
        {"step": step, "perf_counter_s": time.perf_counter()},
    )


def run_fault(args: argparse.Namespace, rank: int, world_size: int) -> None:
    if args.mode != "ddp":
        raise ValueError("fault action requires --mode ddp")
    if world_size < 2:
        raise ValueError("fault action requires at least two processes")
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    checkpoint = work / "ckpt.pt"
    checkpoint_event = work / "checkpoint_event.json"
    failure_marker = work / "failure.triggered"
    failure_event = work / "failure_event.json"
    recovery_event = work / "recovery_event.json"

    train_dataset = load_split(args.data, "train", n=N_TRAIN)
    test_dataset = load_split(args.data, "test")
    unwrapped = Net()
    optimizer = torch.optim.SGD(unwrapped.parameters(), lr=args.lr, momentum=0.9)
    start_epoch = 0
    skip_batches = 0
    global_step = 0
    losses: list[dict[str, Any]] = []
    if checkpoint.exists():
        state = torch.load(checkpoint, map_location="cpu", weights_only=False)
        unwrapped.load_state_dict(state["model"])
        optimizer.load_state_dict(state["opt"])
        start_epoch = int(state["epoch"])
        skip_batches = int(state["step_in_epoch"])
        global_step = int(state["step"])
        losses = list(state["losses"])

    model: nn.Module = DistributedDataParallel(unwrapped, bucket_cap_mb=25)
    local_batch = args.global_batch // world_size
    loader, sampler = make_loader(
        train_dataset,
        local_batch=local_batch,
        rank=rank,
        world_size=world_size,
        distributed=True,
    )
    restart_count = int(os.environ.get("TORCHELASTIC_RESTART_COUNT", "0"))
    model.train()
    started = time.perf_counter()
    for epoch in range(start_epoch, args.epochs):
        if sampler is None:
            raise AssertionError("fault run must have DistributedSampler")
        sampler.set_epoch(epoch)
        for zero_based_index, (images, labels) in enumerate(loader):
            if epoch == start_epoch and zero_based_index < skip_batches:
                continue
            prospective_step = global_step + 1
            if (
                prospective_step == args.failure_step
                and rank == world_size - 1
                and not failure_marker.exists()
            ):
                last_checkpoint = (
                    read_json(checkpoint_event)
                    if checkpoint_event.exists()
                    else {"step": 0, "perf_counter_s": started}
                )
                failure_marker.write_text("triggered\n", encoding="utf-8")
                atomic_json(
                    failure_event,
                    {
                        "failure_step": prospective_step,
                        "failure_rank": rank,
                        "failure_perf_counter_s": time.perf_counter(),
                        "last_checkpoint_step": int(last_checkpoint["step"]),
                        "last_checkpoint_perf_counter_s": float(
                            last_checkpoint["perf_counter_s"]
                        ),
                    },
                )
                os._exit(1)

            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = nn.functional.cross_entropy(logits, labels)
            loss.backward()
            optimizer.step()
            global_step = prospective_step
            mean_loss = global_mean_loss(loss, world_size, args.mode)
            if rank == 0:
                losses.append(
                    {
                        "step": global_step,
                        "epoch": epoch,
                        "step_in_epoch": zero_based_index + 1,
                        "loss": mean_loss,
                    }
                )
                if restart_count > 0 and not recovery_event.exists():
                    atomic_json(
                        recovery_event,
                        {
                            "first_recovered_step": global_step,
                            "perf_counter_s": time.perf_counter(),
                        },
                    )
            if global_step % args.checkpoint_interval == 0:
                if rank == 0:
                    save_checkpoint(
                        checkpoint=checkpoint,
                        checkpoint_event=checkpoint_event,
                        model=model,
                        optimizer=optimizer,
                        epoch=epoch,
                        step_in_epoch=zero_based_index + 1,
                        step=global_step,
                        losses=losses,
                    )
                dist.barrier()
        skip_batches = 0

    training_duration_s = time.perf_counter() - started
    if rank == 0:
        quality = evaluate(base_model(model), test_dataset)
        failure = read_json(failure_event)
        recovery = read_json(recovery_event)
        t_f = float(failure["failure_perf_counter_s"])
        t_c = float(failure["last_checkpoint_perf_counter_s"])
        t_r = float(recovery["perf_counter_s"])
        result = {
            "kind": "fault_recovery",
            "mode": args.mode,
            "world_size": world_size,
            "global_batch": args.global_batch,
            "local_batch": local_batch,
            "learning_rate": args.lr,
            "epochs": args.epochs,
            "optimizer_steps": global_step,
            "checkpoint_interval": args.checkpoint_interval,
            "failure_step": args.failure_step,
            "failure_rank": failure["failure_rank"],
            "last_checkpoint_step": failure["last_checkpoint_step"],
            "repeated_steps": args.failure_step - 1 - int(
                failure["last_checkpoint_step"]
            ),
            "restart_count": restart_count,
            "restart_delay_s": args.restart_delay_s,
            "first_recovered_step": recovery["first_recovered_step"],
            "checkpoint_to_failure_s": t_f - t_c,
            "failure_to_first_recovered_step_s": t_r - t_f,
            "t_lost_s": (t_f - t_c) + (t_r - t_f),
            "training_duration_after_final_start_s": training_duration_s,
            "acc": quality["acc"],
            "auc": quality["auc"],
            "losses": losses,
            "model": model_metadata(),
            "runtime": runtime_metadata(),
        }
        atomic_json(args.output, result)
        print(json.dumps({k: v for k, v in result.items() if k != "losses"}), flush=True)
    dist.barrier()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("speed", "train", "fault"), required=True)
    parser.add_argument(
        "--mode",
        choices=("single", "manual", "ddp", "ddp-no-sync"),
        required=True,
    )
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--global-batch", type=int, default=GLOBAL_BATCH)
    parser.add_argument("--lr", type=float, default=BASE_LR)
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--warmup-steps", type=int, default=10)
    parser.add_argument("--measure-steps", type=int, default=50)
    parser.add_argument("--checkpoint-interval", type=int, default=CHECKPOINT_INTERVAL)
    parser.add_argument("--failure-step", type=int, default=FAILURE_STEP)
    parser.add_argument("--restart-delay-s", type=float, default=5.0)
    parser.add_argument("--work-dir", type=Path, default=Path("lab6/work/fault"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_runtime(args.threads)
    if (
        args.action == "fault"
        and int(os.environ.get("TORCHELASTIC_RESTART_COUNT", "0")) > 0
    ):
        # Give the failed gloo full-mesh sockets time to leave TIME_WAIT before
        # the restarted workers create a fresh process group on the same host.
        time.sleep(args.restart_delay_s)
    rank, world_size = init_distributed(args.mode)
    try:
        if args.action == "speed":
            run_speed(args, rank, world_size)
        elif args.action == "train":
            run_train(args, rank, world_size)
        elif args.action == "fault":
            run_fault(args, rank, world_size)
        else:
            raise AssertionError(args.action)
    finally:
        cleanup_distributed(args.mode)


if __name__ == "__main__":
    main()
