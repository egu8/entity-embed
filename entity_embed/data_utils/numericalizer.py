import inspect
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Union

import numpy as np
import regex
import torch
from cached_property import cached_property
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)

DEFAULT_ALPHABET = list("0123456789abcdefghijklmnopqrstuvwxyz!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ ")


class FieldType(Enum):
    STRING = "string"
    MULTITOKEN = "multitoken"
    SEMANTIC = "semantic"


@dataclass
class FieldConfig:
    key: Union[str, List[str]]
    field_type: FieldType
    tokenizer: Callable[[str], List[str]]
    alphabet: List[str]
    max_str_len: int
    n_channels: int
    embed_dropout_p: float
    use_attention: bool
    n_transformer_layers: int

    @property
    def is_multitoken(self):
        field_type = self.field_type
        if isinstance(field_type, str):
            field_type = FieldType[field_type]
        return field_type == FieldType.MULTITOKEN

    @property
    def is_semantic(self):
        field_type = self.field_type
        if isinstance(field_type, str):
            field_type = FieldType[field_type]
        return field_type == FieldType.SEMANTIC

    @cached_property
    def transformer_tokenizer(self):
        return build_default_transformer_tokenizer()

    def __repr__(self):
        repr_dict = {}
        for k, v in self.__dict__.items():
            if k == "transformer_tokenizer":
                continue

            if isinstance(v, Callable):
                repr_dict[k] = f"{inspect.getmodule(v).__name__}.{v.__name__}"
            else:
                repr_dict[k] = v
        return "{cls}({attrs})".format(
            cls=self.__class__.__name__,
            attrs=", ".join("{}={!r}".format(k, v) for k, v in repr_dict.items()),
        )


# Unicode \w without _ is [\w--_]
tokenizer_re = regex.compile(r"[\w--_]+|[^[\w--_]\s]+", flags=regex.V1)


def default_tokenizer(val):
    return tokenizer_re.findall(val)


def build_default_transformer_tokenizer():
    return AutoTokenizer.from_pretrained("distilbert-base-uncased")


def _record_to_str(keys, record):
    val_list = []
    for key in keys:
        val = record[key]
        # add COL-VAL
        val_list.append("COL")
        # force lowercase, avoids injection of special tokens
        val_list.append(key.lower())
        val_list.append("VAL")
        # force lowercase, avoids injection of special tokens
        val_list.append(val.lower())

    return " ".join(val_list)


class SemanticNumericalizer:
    def __init__(self, field_config):
        self.keys = field_config.key
        self.transformer_tokenizer = field_config.transformer_tokenizer

    def build_tensor(self, record):
        semantic_str = _record_to_str(self.keys, record)
        t = self.transformer_tokenizer.encode(
            semantic_str, padding=False, add_special_tokens=True, return_tensors="pt"
        ).view(-1)
        return t, sum(len(record[key]) for key in self.keys)


class StringNumericalizer:
    def __init__(self, field_config):
        self.key = field_config.key
        self.alphabet = field_config.alphabet
        self.max_str_len = field_config.max_str_len
        self.char_to_ord = {c: i for i, c in enumerate(self.alphabet)}

    def _ord_encode(self, val):
        ord_encoded = []
        for c in val:
            try:
                ord_ = self.char_to_ord[c]
                ord_encoded.append(ord_)
            except KeyError:
                logger.warning(f"Found out of alphabet char at val={val}, char={c}")
        return ord_encoded

    def _build_tensor_from_val(self, val):
        # encoded_arr is a one hot encoded bidimensional tensor
        # with characters as rows and positions as columns.
        # This is the shape expected by StringEmbedCNN.
        ord_encoded_val = self._ord_encode(val)
        encoded_arr = np.zeros((len(self.alphabet), self.max_str_len), dtype=np.float32)
        if len(ord_encoded_val) > 0:
            encoded_arr[ord_encoded_val, range(len(ord_encoded_val))] = 1.0
        t = torch.from_numpy(encoded_arr)
        return t, len(val)

    def build_tensor(self, record):
        # encoded_arr is a one hot encoded bidimensional tensor
        # with characters as rows and positions as columns.
        # This is the shape expected by StringEmbedCNN.
        val = record[self.key]
        return self._build_tensor_from_val(val)


class MultitokenNumericalizer:
    def __init__(self, field_config):
        self.key = field_config.key
        self.tokenizer = field_config.tokenizer
        self.string_numericalizer = StringNumericalizer(field_config=field_config)

    def build_tensor(self, record):
        val_tokens = self.tokenizer(record[self.key])
        t_list = []
        for v in val_tokens:
            if v != "":
                t, __ = self.string_numericalizer._build_tensor_from_val(v)
                t_list.append(t)

        if len(t_list) > 0:
            return torch.stack(t_list), len(t_list)
        else:
            t, __ = self.string_numericalizer._build_tensor_from_val("")
            return torch.stack([t]), 0


class RecordNumericalizer:
    def __init__(
        self,
        field_config_dict,
        field_to_numericalizer,
    ):
        self.field_config_dict = field_config_dict
        self.field_to_numericalizer = field_to_numericalizer

    def build_tensor_dict(self, record):
        tensor_dict = {}
        sequence_length_dict = {}

        for field, numericalizer in self.field_to_numericalizer.items():
            t, sequence_length = numericalizer.build_tensor(record)
            tensor_dict[field] = t
            sequence_length_dict[field] = sequence_length

        return tensor_dict, sequence_length_dict

    def __repr__(self):
        return f"<RecordNumericalizer with field_config_dict={self.field_config_dict}>"


class PairNumericalizer:
    def __init__(self, field_list):
        self.field_list = field_list
        self.transformer_tokenizer = build_default_transformer_tokenizer()

    def _record_batch_to_str_batch(self, record_batch):
        return [_record_to_str(keys=self.field_list, record=record) for record in record_batch]

    def build_tensor_batch(self, record_batch_left, record_batch_right):
        str_batch_left = self._record_batch_to_str_batch(record_batch_left)
        str_batch_right = self._record_batch_to_str_batch(record_batch_right)

        return self.transformer_tokenizer(
            text=str_batch_left,
            text_pair=str_batch_right,
            padding=True,
            add_special_tokens=True,
            return_tensors="pt",
        )

    def __repr__(self):
        return f"<PairNumericalizer with field_list={self.field_list}>"
