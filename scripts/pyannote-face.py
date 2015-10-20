#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2015 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

"""Face detection and tracking

Usage:
  pyannote-face detect [--verbose] [options] <video> <output>
  pyannote-face track [--verbose] <video> <shot> <detection> <output>
  pyannote-face shape [--verbose] <video> <model> <tracking> <output>
  pyannote-face (-h | --help)
  pyannote-face --version

Options:
  --every=<msec>       Process one frame every <msec> milliseconds.
  --smallest=<size>    (approximate) size of smallest face [default: 36].
  -h --help            Show this screen.
  --version            Show version.
  --verbose            Show progress.
"""

from docopt import docopt
from pyannote.video import __version__
from pyannote.video import Video

from tqdm import tqdm
from munkres import Munkres
import numpy as np
import cv2

import dlib

SMALLEST_DEFAULT = 36

FACE_TEMPLATE = ('{t:.3f} {identifier:d} '
                 '{left:d} {top:d} {right:d} {bottom:d} '
                 '{confidence:.3f}\n')


def getShotGenerator(shotFile):
    """Parse precomputed shot file and generate boundary timestamps"""

    t = yield

    with open(shotFile, 'r') as f:

        for line in f:
            T = float(line.strip())

            while True:
                # loop until a large enough t is sent to the generator
                if T > t:
                    t = yield
                    continue

                # else, we found a new shot
                t = yield T
                break


def getFaceGenerator(detection):
    """Parse precomputed face file and generate timestamped faces"""

    # t is the time sent by the frame generator
    t = yield

    with open(detection, 'r') as f:

        faces = []
        currentT = None

        for line in f:

            # parse line
            # time, identifier, left, top, right, bottom, confidence
            tokens = line.strip().split()
            T = float(tokens[0])
            identifier = int(tokens[1])
            face = dlib.drectangle(*[int(token) for token in tokens[2:6]])

            # load all faces from current frame
            # and only those faces
            if T == currentT or currentT is None:
                faces.append((identifier, face))
                currentT = T
                continue

            # once all faces at current time are loaded
            # wait until t reaches current time
            # then returns all faces at once

            while True:

                # wait...
                if currentT > t:
                    t = yield t, []
                    continue

                # return all faces at once
                t = yield currentT, faces

                # reset current time and corresponding faces
                faces = [(identifier, face)]
                currentT = T
                break

        while True:
            t = yield t, []


def detect(video, output, step=None, upscale=1, show_progress=False):
    """Face detection"""

    # frame iterator
    generator = video.iterframes(step=step, with_time=True)
    if show_progress:

        if step is None:
            total = video.duration * video.fps
        else:
            total = video.duration / step

        generator = tqdm(iterable=generator,
                         total=total,
                         leave=True, mininterval=1.,
                         unit='frames', unit_scale=True)

    # face detector
    faceDetector = dlib.get_frontal_face_detector()

    identifier = 0

    with open(output, 'w') as foutput:

        for t, frame in generator:

            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

            for boundingBox in faceDetector(gray, upscale):

                identifier = identifier + 1

                foutput.write(FACE_TEMPLATE.format(
                    t=t, identifier=identifier, confidence=-1,
                    left=boundingBox.left(), right=boundingBox.right(),
                    top=boundingBox.top(), bottom=boundingBox.bottom()))


def track(video, shot, detection, output, show_progress=False):
    """Tracking by detection"""

    # frame generator
    frames = video.iterframes(with_time=True)
    if show_progress:
        frames = tqdm(iterable=frames,
                      total=video.duration * video.fps,
                      leave=True, mininterval=1.,
                      unit='frames', unit_scale=True)

    # shot generator
    shotGenerator = getShotGenerator(shot)
    shotGenerator.send(None)

    # face generator
    faceGenerator = getFaceGenerator(detection)
    faceGenerator.send(None)

    # Hungarian algorithm for face/tracker matching
    hungarian = Munkres()

    trackers = dict()
    confidences = dict()
    identifier = 0

    with open(output, 'w') as foutput:

        for timestamp, frame in frames:

            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

            shot = shotGenerator.send(timestamp)

            # reset trackers at shot boundaries
            if shot:
                trackers.clear()
                confidences.clear()

            # get all detected faces at this time
            T, faces = faceGenerator.send(timestamp)
            # not that T might be differ slightly from t
            # due to different steps in frame iteration

            # update all trackers and store their confidence
            for i, tracker in trackers.items():
                confidences[i] = tracker.update(gray)

            unmatched = set(trackers)

            Nt, Nf = len(trackers), len(faces)
            if Nt and Nf:

                # compute intersection for every tracker/face pair
                N = max(Nt, Nf)
                areas = np.zeros((N, N))
                trackers_ = trackers.items()
                for t, (i, tracker) in enumerate(trackers_):
                    position = tracker.get_position()
                    for f, (_, face) in enumerate(faces):
                        areas[t, f] = position.intersect(face).area()

                # find the best one-to-one mapping
                mapping = hungarian.compute(np.max(areas) - areas)

                for t, f in mapping:

                    if t >= Nt or f >= Nf:
                        continue

                    area = areas[t, f]

                    _, face = faces[f]
                    faceArea = face.area()

                    i, tracker = trackers_[t]
                    trackerArea = tracker.get_position().area()

                    # if enough overlap,
                    # re-intialize tracker and mark face as matched
                    if (2 * area > faceArea) or (2 * area > trackerArea):
                        tracker.start_track(gray, face)
                        unmatched.remove(i)

                        foutput.write(FACE_TEMPLATE.format(
                            t=T, identifier=i, confidence=confidences[i],
                            left=int(face.left()), right=int(face.right()),
                            top=int(face.top()), bottom=int(face.bottom())))

                        faces[f] = None, None

            for _, face in faces:

                # this face was matched already
                if face is None:
                    continue

                # new tracker
                identifier = identifier + 1
                tracker = dlib.correlation_tracker()
                tracker.start_track(gray, face)
                confidences[identifier] = tracker.update(gray)
                trackers[identifier] = tracker

                foutput.write(FACE_TEMPLATE.format(
                    t=T, identifier=identifier, confidence=0.000,
                    left=int(face.left()), right=int(face.right()),
                    top=int(face.top()), bottom=int(face.bottom())))

            for i, tracker in trackers.items():

                if i not in unmatched:
                    continue

                face = tracker.get_position()

                foutput.write(FACE_TEMPLATE.format(
                    t=T, identifier=i, confidence=confidences[i],
                    left=int(face.left()), right=int(face.right()),
                    top=int(face.top()), bottom=int(face.bottom())))


def shape(video, model, tracking, output, show_progress=False):
    """Facial features detection"""

    # frame generator
    frames = video.iterframes(with_time=True)
    if show_progress:
        frames = tqdm(iterable=frames,
                      total=video.duration * video.fps,
                      leave=True, mininterval=1.,
                      unit='frames', unit_scale=True)

    # face generator
    faceGenerator = getFaceGenerator(tracking)
    faceGenerator.send(None)

    # facial features detector
    facialFeaturesDetector = dlib.shape_predictor(model)

    with open(output, 'w') as foutput:

        for timestamp, frame in frames:

            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

            # get all detected faces at this time
            T, faces = faceGenerator.send(timestamp)
            # not that T might be differ slightly from t
            # due to different steps in frame iteration

            for identifier, face in faces:

                boundingBox = dlib.rectangle(
                    int(face.left()), int(face.top()),
                    int(face.right()), int(face.bottom()))
                points = facialFeaturesDetector(gray, boundingBox)
                facialFeatures = [(p.x, p.y) for p in points.parts()]

                foutput.write('{t:.3f} {identifier:d}'.format(
                    t=T, identifier=identifier))
                for x, y in facialFeatures:
                    foutput.write(' {x:d} {y:d}'.format(x=x, y=y))
                foutput.write('\n')


if __name__ == '__main__':

    # parse command line arguments
    version = 'pyannote-face {version}'.format(version=__version__)
    arguments = docopt(__doc__, version=version)

    # initialize video
    video = Video(arguments['<video>'])

    verbose = arguments['--verbose']

    # face detection
    if arguments['detect']:

        # every xxx milliseconds
        every = arguments['--every']
        if not every:
            step = None
        else:
            step = 1e-3 * float(arguments['--every'])

        # (approximate) size of smallest face
        smallest = float(arguments['--smallest'])
        if smallest > SMALLEST_DEFAULT:
            upscale = 1
        else:
            upscale = int(np.ceil(SMALLEST_DEFAULT / smallest))

        output = arguments['<output>']

        detect(video, output,
               step=step, upscale=upscale,
               show_progress=verbose)

    # face tracking
    if arguments['track']:

        shot = arguments['<shot>']
        detection = arguments['<detection>']
        output = arguments['<output>']
        track(video, shot, detection, output,
              show_progress=verbose)

    # facial features detection
    if arguments['shape']:

        tracking = arguments['<tracking>']
        model = arguments['<model>']
        output = arguments['<output>']
        shape(video, model, tracking, output,
              show_progress=verbose)
