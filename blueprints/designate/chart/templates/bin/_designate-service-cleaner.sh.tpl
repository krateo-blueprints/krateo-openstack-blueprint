#!/bin/bash
# Copyright (c) 2025 VEXXHOST, Inc.
# SPDX-License-Identifier: Apache-2.0

set -ex

designate-manage \
  --config-file /etc/designate/designate.conf $([ -d /etc/designate/designate.conf.d ] && echo --config-dir /etc/designate/designate.conf.d) \
  --config-dir /etc/designate/designate.conf.d \
  service clean
