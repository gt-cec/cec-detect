# CEC Detect

Object detection and segmentation made easy!

This package contains a `Detector` class that wraps around OWLv2 and SAM2 to extract objects and segmentation masks.

## Install

Clone the repository, ideally not in your project codebase:

`git clone https://github.com/gt-cec/cec-detect`

Install the package:

`pip install -e cec-detect`

## Usage

Using the package is as simple as initializing the class.

```
import cec_detect

detector = cec_detect.Detector()

image = ...  # RGB numpy array
classes = ["person", "dog", "cat"]  # classes to detect
detector.
