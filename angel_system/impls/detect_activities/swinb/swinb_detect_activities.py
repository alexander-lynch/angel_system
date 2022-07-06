import importlib.util
import logging
from typing import Iterable, Dict, Hashable, List, Union, Any

import numpy as np
import torch
import torchvision

from angel_system.interfaces.detect_activities import DetectActivities
from angel_system.impls.detect_activities.swinb.swin import swin_b
from angel_system.impls.detect_activities.swinb.utils import (
    get_start_end_idx, spatial_sampling, temporal_sampling
)


LOG = logging.getLogger(__name__)


class SwinBTransformer(DetectActivities):
    """
    ``DetectActivities`` implementation using the Shifted window (Swin)
    transformer from LEARN. The LEARN implementation can be found here:
    https://gitlab.kitware.com/darpa_learn/learn/-/blob/master/learn/algorithms/TimeSformer/models/swin.py

    :param checkpoint_path: Path to a saved checkpoint file containing
        weights for the model.
    :param num_classes: Number of classes the model was trained on. This
        should match the number of classes the model checkpoint was trained
        on.
    :param labels_file: Path to the labels file for the given checkpoint.
        The labels file is a text file with the class labels, one class
        per line. This should match the class labels the model checkpoint
        was trained on.
    :param num_frames: Number of frames passed to the model for inference.
    :param sampling_rate: Sampling rate for the frame input. For example,
        if this is set to 2 and num_frames is set to 32, the activity
        detector should pass 64 frames as input to the detect activities
        function.
    :param torch_device: When using CUDA, use the device by the given ID. By
        default, this is set to `cpu`.
    :param det_threshold: Threshold for which predictions must exceed to
        create an activity detection.
    """

    def __init__(
        self,
        checkpoint_path: str,
        num_classes: int,
        labels_file: str,
        num_frames: int = 32,
        sampling_rate: int = 2,
        torch_device: str = "cpu",
        det_threshold: float = 0.75,
    ):
        self._checkpoint_path = checkpoint_path
        self._num_classes = num_classes
        self._torch_device = torch_device
        self._det_threshold = det_threshold
        self._num_frames = num_frames
        self._sampling_rate = sampling_rate
        self._labels_file = labels_file

        # Set to None for lazy loading later.
        self._model: torch.nn.Module = None  # type: ignore
        self._model_device: torch.device = None  # type: ignore

        # Default configs from learn SwinVideo config
        self._mean = [0.45, 0.45, 0.45]
        self._std = [0.225, 0.225, 0.225]
        self._crop_size = 224
        self._frames_per_second = 30

        # Transfrom from learn/TimeSformer/video_classification.py
        self.transform = torchvision.transforms.Compose(
            [
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Normalize(
                    self._mean, self._std
                )
            ]
        )

        # Set up the labels from the given labels file
        self._labels = []
        with open(self._labels_file, "r") as f:
            for line in f:
                self._labels.append(line.rstrip())

    def get_model(self) -> torch.nn.Module:
        """
        Lazy load the torch model in an idempotent manner.
        :raises RuntimeError: Use of CUDA was requested but is not available.
        """
        model = self._model
        if model is None:
            # Load the model with the checkpoint
            model = swin_b(self._checkpoint_path, self._num_classes)
            model = model.eval()

            # Transfer the model to the requested device
            if self._torch_device != 'cpu':
                if torch.cuda.is_available():
                    model_device = torch.device(device=self._torch_device)
                    model = model.to(device=model_device)
                else:
                    raise RuntimeError(
                        "Use of CUDA requested but not available."
                    )
            else:
                model_device = torch.device(self._torch_device)

            self._model = model
            self._model_device = model_device

        return model

    def detect_activities(
        self,
        frame_iter: Iterable[np.ndarray]
    ) -> Iterable[str]:
        """
        Formats the given iterable of frames into the required input format
        for the swin model and then inputs them to the model for inferencing.
        """
        # Check that we got the right number of frames
        assert len(frame_iter) == (self._sampling_rate * self._num_frames)
        model = self.get_model()

        # Form the frames into the required format for the video model
        # Based off of the Learn swin CollateFn
        spatial_idx = 1 # only perform uniform crop and short size jitter
        clip_idx = -1

        frames = [self.transform(f) for f in frame_iter]
        frames = [torch.stack(frames)]

        clip_size = (((self._sampling_rate * self._num_frames) / self._frames_per_second)
                     * self._frames_per_second)
        start_end_idx = [
            get_start_end_idx(len(x), clip_size, clip_idx=clip_idx, num_clips=1)
            for x in frames
        ]

        # This subsamples every n (sample rate) frames
        frames = [
            temporal_sampling(x, s, e, self._num_frames)
            for x, (s, e) in zip(frames, start_end_idx)
        ]

        # Crop and random short side scale jitter
        # NOTE: We are passing the same value for min scale, max scale,
        # and crop size meaning that the random short side scale
        # transform is deterministic.
        frames = [x.permute(1, 0, 2, 3) for x in frames]
        frames = [spatial_sampling(x,
                                   spatial_idx=spatial_idx,
                                   min_scale=self._crop_size,
                                   max_scale=self._crop_size,
                                   crop_size=self._crop_size) for x in frames]
        frames = torch.stack(frames)

        # Move the inputs to the GPU if necessary
        if self._model.cuda:
            frames = frames.cuda()

        # Predict!
        with torch.no_grad():
            preds = self._model(frames)

        # Get the top predicted classes
        post_act = torch.nn.Softmax(dim=1)
        preds: torch.Tensor = post_act(preds) # shape: (1, num_classes)
        top_preds = preds.topk(k=5)

        # Map the predicted classes to the label names
        # top_preds.indices is a 1xk tensor
        pred_class_indices = top_preds.indices[0]

        pred_class_names = [self._labels[int(i)] for i in pred_class_indices]

        # Filter out any detections below the threshold
        predictions = []
        pred_values = top_preds.values[0]
        for idx, p in enumerate(pred_class_names):
            if (pred_values[idx] > self._det_threshold):
                predictions.append(p)

        return predictions

    def get_config(self) -> dict:
        return {
            "cuda_device": self._cuda_device,
            "num_classes": self._num_classes,
            "det_threshold": self._det_threshold,
            "checkpoint_path": self._checkpoint_path,
            "num_frames": self._num_frames,
            "sampling_rate": self._sampling_rate,
            "labels_file": self._labels_file,
        }

    @classmethod
    def is_usable(cls) -> bool:
        # Only torch/torchvision required
        return True
