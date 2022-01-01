#!/usr/bin/env bash

FEATURE_DIR=/custom/hifiberry

mkdir -p $FEATURE_DIR
touch $FEATURE_DIR/analoginput.feature

echo analoginput.feature added to ${FEATURE_DIR}. Run /opt/hifiberry/bin/reconfigure-players to take effect
