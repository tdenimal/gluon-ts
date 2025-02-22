# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

# Standard library imports
from typing import List, Optional

# Third-party imports
from mxnet.gluon import HybridBlock
from pandas.tseries.frequencies import to_offset

# First-party imports
from gluonts.core.component import validated
from gluonts.dataset.field_names import FieldName
from gluonts.model.deepstate.issm import ISSM, CompositeISSM
from gluonts.model.estimator import GluonEstimator
from gluonts.model.predictor import Predictor, RepresentableBlockPredictor
from gluonts.support.util import copy_parameters
from gluonts.time_feature.lag import (
    TimeFeature,
    time_features_from_frequency_str,
)
from gluonts.trainer import Trainer
from gluonts.transform import (
    AddObservedValuesIndicator,
    AddAgeFeature,
    AddTimeFeatures,
    AsNumpyArray,
    Chain,
    CanonicalInstanceSplitter,
    ExpandDimArray,
    RemoveFields,
    SetField,
    TestSplitSampler,
    Transformation,
    VstackFeatures,
)

# Relative imports
from ._network import DeepStatePredictionNetwork, DeepStateTrainingNetwork

SEASON_INDICATORS_FIELD = "seasonal_indicators"


# A dictionary mapping granularity to the period length of the longest season
# one can expect given the granularity of the time series.
# This is similar to the frequency value in the R forecast package:
# https://stats.stackexchange.com/questions/120806/frequency-value-for-seconds-minutes-intervals-data-in-r
# This is useful for setting default values for past/context length for models
# that do not do data augmentation and uses a single training example per time series in the dataset.
FREQ_LONGEST_PERIOD_DICT = {
    "M": 12,  # yearly seasonality
    "W-SUN": 52,  # yearly seasonality
    "D": 365,  # yearly seasonality
    "B": 365,  # yearly seasonality
    "H": 168,  # weekly seasonality
    "T": 1440,  # daily seasonality
}


def longest_period_from_frequency_str(freq_str: str) -> int:
    offset = to_offset(freq_str)
    return FREQ_LONGEST_PERIOD_DICT[offset.name] // offset.n


class DeepStateEstimator(GluonEstimator):
    """
    Construct a DeepState estimator.
    
    This implements the deep state space model described in
    [RSG+18]_.

    Parameters
    ----------
    freq
        Frequency of the data to train on and predict
    prediction_length
        Length of the prediction horizon
    add_trend
        Flag to indicate whether to include trend component in the
        state space model
    past_length
        This is the length of the training time series;
        i.e., number of steps to unroll the RNN for before computing predictions.
        Set this to (at most) the length of the shortest time series in the dataset.
        (default: None, in which case the training length is set such that at least
        `num_seasons_to_train` seasons are included in the training.
        See `num_seasons_to_train`)
    num_periods_to_train
        (Used only when `past_length` is not set)
        Number of periods to include in the training time series. (default: 4)
        Here period corresponds to the longest cycle one can expect given the granularity of the time series.
        See: https://stats.stackexchange.com/questions/120806/frequency-value-for-seconds-minutes-intervals-data-in-r
    trainer
        Trainer object to be used (default: Trainer())
    num_layers
        Number of RNN layers (default: 2)
    num_cells
        Number of RNN cells for each layer (default: 40)
    cell_type
        Type of recurrent cells to use (available: 'lstm' or 'gru';
        default: 'lstm')
    num_eval_samples
        Number of samples paths to draw when computing predictions
        (default: 100)
    dropout_rate
        Dropout regularization parameter (default: 0.1)
    use_feat_dynamic_real
        Whether to use the ``feat_dynamic_real`` field from the data
        (default: False)
    use_feat_static_cat
        Whether to use the ``feat_static_cat`` field from the data
        (default: False)
    cardinality
        Number of values of each categorical feature.
        This must be set if ``use_feat_static_cat == True`` (default: None)
    embedding_dimension
        Dimension of the embeddings for categorical features (the same
        dimension is used for all embeddings, default: 20)
    scaling
        Whether to automatically scale the target values (default: true)
    time_features
        Time features to use as inputs of the RNN (default: None, in which
        case these are automatically determined based on freq)
    """

    @validated()
    def __init__(
        self,
        freq: str,
        prediction_length: int,
        add_trend: bool = False,
        past_length: Optional[int] = None,
        num_periods_to_train: int = 4,
        trainer: Trainer = Trainer(epochs=25, hybridize=False),
        num_layers: int = 2,
        num_cells: int = 40,
        cell_type: str = "lstm",
        num_eval_samples: int = 100,
        dropout_rate: float = 0.1,
        use_feat_dynamic_real: bool = False,
        use_feat_static_cat: bool = False,
        cardinality: Optional[List[int]] = None,
        embedding_dimension: int = 20,
        issm: Optional[ISSM] = None,
        scaling: bool = True,
        time_features: Optional[List[TimeFeature]] = None,
    ) -> None:
        super().__init__(trainer=trainer)

        assert (
            prediction_length > 0
        ), "The value of `prediction_length` should be > 0"
        assert (
            past_length is None or past_length > 0
        ), "The value of `past_length` should be > 0"
        assert num_layers > 0, "The value of `num_layers` should be > 0"
        assert num_cells > 0, "The value of `num_cells` should be > 0"
        assert (
            num_eval_samples > 0
        ), "The value of `num_eval_samples` should be > 0"
        assert dropout_rate >= 0, "The value of `dropout_rate` should be >= 0"
        assert (
            cardinality is not None or not use_feat_static_cat
        ), "You must set `cardinality` if `use_feat_static_cat=True`"
        assert cardinality is None or [
            c > 0 for c in cardinality
        ], "Elements of `cardinality` should be > 0"
        assert (
            embedding_dimension > 0
        ), "The value of `embedding_dimension` should be > 0"

        self.freq = freq
        self.past_length = (
            past_length
            if past_length is not None
            else num_periods_to_train * longest_period_from_frequency_str(freq)
        )
        self.prediction_length = prediction_length
        self.add_trend = add_trend
        self.num_layers = num_layers
        self.num_cells = num_cells
        self.cell_type = cell_type
        self.num_sample_paths = num_eval_samples
        self.scaling = scaling
        self.dropout_rate = dropout_rate
        self.use_feat_dynamic_real = use_feat_dynamic_real
        self.use_feat_static_cat = use_feat_static_cat
        self.cardinality = cardinality if use_feat_static_cat else [1]
        self.embedding_dimension = embedding_dimension

        self.issm = (
            issm
            if issm is not None
            else CompositeISSM.get_from_freq(freq, add_trend)
        )

        self.time_features = (
            time_features
            if time_features is not None
            else time_features_from_frequency_str(self.freq)
        )

    def create_transformation(self) -> Transformation:
        remove_field_names = [
            FieldName.FEAT_DYNAMIC_CAT,
            FieldName.FEAT_STATIC_REAL,
        ]
        if not self.use_feat_dynamic_real:
            remove_field_names.append(FieldName.FEAT_DYNAMIC_REAL)

        return Chain(
            [RemoveFields(field_names=remove_field_names)]
            + (
                [SetField(output_field=FieldName.FEAT_STATIC_CAT, value=[0.0])]
                if not self.use_feat_static_cat
                else []
            )
            + [
                AsNumpyArray(field=FieldName.FEAT_STATIC_CAT, expected_ndim=1),
                AsNumpyArray(field=FieldName.TARGET, expected_ndim=1),
                # gives target the (1, T) layout
                ExpandDimArray(field=FieldName.TARGET, axis=0),
                AddObservedValuesIndicator(
                    target_field=FieldName.TARGET,
                    output_field=FieldName.OBSERVED_VALUES,
                ),
                # Unnormalized seasonal features
                AddTimeFeatures(
                    time_features=CompositeISSM.seasonal_features(self.freq),
                    pred_length=self.prediction_length,
                    start_field=FieldName.START,
                    target_field=FieldName.TARGET,
                    output_field=SEASON_INDICATORS_FIELD,
                ),
                AddTimeFeatures(
                    start_field=FieldName.START,
                    target_field=FieldName.TARGET,
                    output_field=FieldName.FEAT_TIME,
                    time_features=self.time_features,
                    pred_length=self.prediction_length,
                ),
                AddAgeFeature(
                    target_field=FieldName.TARGET,
                    output_field=FieldName.FEAT_AGE,
                    pred_length=self.prediction_length,
                    log_scale=True,
                ),
                VstackFeatures(
                    output_field=FieldName.FEAT_TIME,
                    input_fields=[FieldName.FEAT_TIME, FieldName.FEAT_AGE]
                    + (
                        [FieldName.FEAT_DYNAMIC_REAL]
                        if self.use_feat_dynamic_real
                        else []
                    ),
                ),
                CanonicalInstanceSplitter(
                    target_field=FieldName.TARGET,
                    is_pad_field=FieldName.IS_PAD,
                    start_field=FieldName.START,
                    forecast_start_field=FieldName.FORECAST_START,
                    instance_sampler=TestSplitSampler(),
                    time_series_fields=[
                        FieldName.FEAT_TIME,
                        SEASON_INDICATORS_FIELD,
                        FieldName.OBSERVED_VALUES,
                    ],
                    allow_target_padding=True,
                    instance_length=self.past_length,
                    use_prediction_features=True,
                    prediction_length=self.prediction_length,
                ),
            ]
        )

    def create_training_network(self) -> DeepStateTrainingNetwork:
        return DeepStateTrainingNetwork(
            num_layers=self.num_layers,
            num_cells=self.num_cells,
            cell_type=self.cell_type,
            past_length=self.past_length,
            prediction_length=self.prediction_length,
            issm=self.issm,
            dropout_rate=self.dropout_rate,
            cardinality=self.cardinality,
            embedding_dimension=self.embedding_dimension,
            scaling=self.scaling,
        )

    def create_predictor(
        self, transformation: Transformation, trained_network: HybridBlock
    ) -> Predictor:
        prediction_network = DeepStatePredictionNetwork(
            num_sample_paths=self.num_sample_paths,
            num_layers=self.num_layers,
            num_cells=self.num_cells,
            cell_type=self.cell_type,
            past_length=self.past_length,
            prediction_length=self.prediction_length,
            issm=self.issm,
            dropout_rate=self.dropout_rate,
            cardinality=self.cardinality,
            embedding_dimension=self.embedding_dimension,
            scaling=self.scaling,
            params=trained_network.collect_params(),
        )

        copy_parameters(trained_network, prediction_network)

        return RepresentableBlockPredictor(
            input_transform=transformation,
            prediction_net=prediction_network,
            batch_size=self.trainer.batch_size,
            freq=self.freq,
            prediction_length=self.prediction_length,
            ctx=self.trainer.ctx,
        )
