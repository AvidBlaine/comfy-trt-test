# -*- coding: utf-8 -*-

# modified from https://github.com/NVIDIA/Stable-Diffusion-WebUI-TensorRT/blob/main/model_manager.py
# STATUS: ok i guess

import json
import os
import logging
from dataclasses import dataclass
import torch

from .exporter import get_cc


BASE_PATH = os.path.dirname(os.path.realpath(__file__))
ONNX_MODEL_DIR = os.path.join(BASE_PATH, "Unet-onnx")
if not os.path.exists(ONNX_MODEL_DIR):
	os.makedirs(ONNX_MODEL_DIR)
TRT_MODEL_DIR = os.path.join(BASE_PATH, "Unet-trt")
if not os.path.exists(TRT_MODEL_DIR):
	os.makedirs(TRT_MODEL_DIR)

MODEL_FILE = os.path.join(TRT_MODEL_DIR, "model.json")

cc_major, cc_minor = get_cc()


class ModelManager:
	def __init__(self, model_file=MODEL_FILE) -> None:
		self.all_models = {}
		self.model_file = model_file
		self.cc = f"cc{cc_major}{cc_minor}"
		if not os.path.exists(model_file):
			logging.warning("Model file does not exist. Creating new one.")
		else:
			self.all_models = self.read_json()
		self.update()

	@staticmethod
	def get_onnx_path(model_name):
		onnx_filename = f"{model_name}.onnx"
		onnx_path = os.path.join(ONNX_MODEL_DIR, onnx_filename)
		return onnx_filename, onnx_path

	def get_trt_path(self, model_name, profile, static_shape):
		profile_hash = []
		n_profiles = 1 if static_shape else 3
		for k, v in profile.items():
			dim_hash = []
			for i in range(n_profiles):
				dim_hash.append("x".join([str(x) for x in v[i]]))
			profile_hash.append(k + "=" + "+".join(dim_hash))

		# profile_hash = "-".join(profile_hash)  # need shorter hash coz windows file path length limit
		trt_filename = ("_".join([model_name, self.cc]) + ".trt")
		trt_path = os.path.join(TRT_MODEL_DIR, trt_filename)

		return trt_filename, trt_path

	def update(self):
		trt_engines = [trt_file for trt_file in os.listdir(TRT_MODEL_DIR) if trt_file.endswith(".trt")]

		tmp_all_models = self.all_models.copy()
		for cc, base_models in tmp_all_models.items():
			for base_model, models in base_models.items():
				tmp_config_list = {}
				for model_config in models:
					if model_config["filepath"] not in trt_engines:
						logging.info(f"Model config outdated. {model_config['filepath']} was not found")
						continue
					tmp_config_list[model_config["filepath"]] = model_config

				tmp_config_list = list(tmp_config_list.values())
				if len(tmp_config_list) == 0:
					self.all_models[cc].pop(base_model)
				else:
					self.all_models[cc][base_model] = models

		self.write_json()

	def add_entry(self, model_name, profile, static_shapes, fp32, baseline_model, inpaint, refit, vram, unet_hidden_dim, lora):
		config = ModelConfig(profile, static_shapes, fp32, baseline_model, inpaint, refit, lora, vram, unet_hidden_dim)
		trt_name, trt_path = self.get_trt_path(model_name, profile, static_shapes)

		base_model_name = f"{model_name}"
		if self.cc not in self.all_models:
			self.all_models[self.cc] = {}

		if base_model_name not in self.all_models[self.cc]:
			self.all_models[self.cc][base_model_name] = []
		self.all_models[self.cc][base_model_name].append({"filepath": trt_name, "config": config})

		self.write_json()

	def add_lora_entry(self, base_model, lora_name, trt_lora_path, fp32, baseline_model, inpaint, vram, unet_hidden_dim):
		config = ModelConfig([[], [], []], False, fp32, baseline_model, inpaint, True, True, vram, unet_hidden_dim)
		self.all_models[self.cc][lora_name] = [{"filepath": trt_lora_path, "base_model": base_model, "config": config}]
		self.write_json()

	def write_json(self):
		with open(self.model_file, "w") as f:
			json.dump(self.all_models, f, indent=4, cls=ModelConfigEncoder)

	def read_json(self, encode_config=True):
		with open(self.model_file, "r") as f:
			out = json.load(f)

		if not encode_config:
			return out

		for cc, models in out.items():
			for base_model, configs in models.items():
				for i in range(len(configs)):
					out[cc][base_model][i]["config"] = ModelConfig(**configs[i]["config"])
		return out

	def available_models(self):
		available = self.all_models.get(self.cc, {})
		return available

	def get_timing_cache(self):
		cache = os.path.join(
			BASE_PATH,
			"timing_caches",
			"timing_cache_{}_{}.cache".format("win" if os.name == "nt" else "linux", self.cc),
		)
		return cache

	def get_valid_models_from_dict(self, base_model: str, feed_dict: dict):
		valid_models = []
		distances = []
		models = self.available_models()
		for model in models[base_model]:
			valid, distance = model["config"].is_compatible_from_dict(feed_dict)
			if valid:
				valid_models.append(model)
				distances.append(distance)
		return valid_models, distances

	def get_valid_models(self, base_model: str, width: int, height: int, batch_size: int, max_embedding: int):
		valid_models = []
		distances = []
		models = self.available_models()
		for model in models[base_model]:
			valid, distance = model["config"].is_compatible(width, height, batch_size, max_embedding)
			if valid:
				valid_models.append(model)
				distances.append(distance)
		return valid_models, distances


@dataclass
class ModelConfig:
	profile: dict
	static_shapes: bool
	fp32: bool
	baseline_model: str  # save model info, for values see comfy/supported_models.py, breaking change incompatible A1111
	inpaint: bool
	refit: bool
	lora: bool
	vram: int
	unet_hidden_dim: int = 4

	def is_compatible_from_dict(self, feed_dict: dict):
		distance = 0
		for k, v in feed_dict.items():
			_min, _opt, _max = self.profile[k]
			v_tensor = torch.Tensor(list(v.shape))
			r_min = torch.Tensor(_max) - v_tensor
			r_opt = (torch.Tensor(_opt) - v_tensor).abs()
			r_max = v_tensor - torch.Tensor(_min)
			if torch.any(r_min < 0) or torch.any(r_max < 0):
				return False, distance
			distance += r_opt.sum() + 0.5 * (r_max.sum() + 0.5 * r_min.sum())
		return True, distance

	def is_compatible(self, width: int, height: int, batch_size: int, max_embedding: int):
		distance = 0
		sample = self.profile["sample"]
		embedding = self.profile["encoder_hidden_states"]

		batch_size *= 2
		width //=  8
		height //= 8

		_min, _opt, _max = sample
		_min_em, _opt_em, _max_em = embedding
		if (
			_min[0] > batch_size or _max[0] < batch_size
			or _min[2] > height or _max[2] < height
			or _min[3] > width or _max[3] < width
			or _min_em[1] > max_embedding or _max_em[1] < max_embedding
		):
			return False, distance

		distance = (
			abs(_opt[0] - batch_size)
			+ abs(_opt[2] - height)
			+ abs(_opt[3] - width)
			+ 0.5 * (abs(_max[2] - height) + abs(_max[3] - width))
		)
		return True, distance


class ModelConfigEncoder(json.JSONEncoder):
	def default(self, o: ModelConfig):
		return o.__dict__


modelmanager = ModelManager()
