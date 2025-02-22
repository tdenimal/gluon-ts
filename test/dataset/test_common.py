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

import pytest
import pandas as pd

from gluonts.dataset.common import ProcessStartField


@pytest.mark.parametrize(
    "freq, expected",
    [
        ("B", "2019-11-01"),
        ("W", "2019-10-27"),
        ("M", "2019-10-31"),
        ("12M", "2019-10-31"),
        ("A-DEC", "2018-12-31"),
    ],
)
def test_process_start_field(freq, expected):
    process = ProcessStartField.process
    given = "2019-11-01 12:34:56"

    assert process(given, freq) == pd.Timestamp(expected, freq)
