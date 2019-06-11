from flask import Flask, request
from threading import Thread
import json
import os
from flask import Flask, flash, request, redirect, url_for
import uuid
import numpy as np
from PIL import Image as PilImage

from ilastik.array5d.point5D import Point5D, Slice5D, Shape5D
from ilastik.array5d import Array5D
from ilastik.annotations import Annotation
from ilastik.classifiers.pixel_classifier import PixelClassifier, StrictPixelClassifier
from ilastik.data_source import FlatDataSource
from ilastik.features.vigra_features import GaussianSmoothing, HessianOfGaussian
from ilastik.utility import flatten, unflatten, listify

app = Flask("WebserverHack")
app.config['DATA_DIR'] = '/tmp/flask_stuff/'
app.config['AVAILABLE_FEATURE_EXTRACTORS'] = [GaussianSmoothing, HessianOfGaussian]
app.config['FEATURE_EXTRACTOR_MAP'] = {f.__name__:f for f in app.config['AVAILABLE_FEATURE_EXTRACTORS']}


os.system(f"rm -rfv {app.config['DATA_DIR']}")
os.system(f"mkdir -v {app.config['DATA_DIR']}")

files = {}
data_sources = {}
annotations = {}
classifiers = {}
feature_extractors = {}

class Context:
    objects = {}

    @classmethod
    def create(cls, klass):
        obj = klass.from_json_data(cls.get_request_params())
        uid = uuid.uuid4()
        cls.objects[uid] = obj
        return obj, str(uid)

    @classmethod
    def get(cls, key):
        uid = uuid.UUID(str(key))
        return cls.objects[uid]

    @classmethod
    def loadObject(cls, uid:str):
        try:
            return cls.get(uid)
        except KeyError:
            raise ValueError(uid)

    @classmethod
    def deserialize(cls, value:str):
        for deserializer in [cls.loadObject, int, float]:
            try:
                return deserializer(value)
            except ValueError:
                pass
        return value

    @classmethod
    def get_request_params(cls):
        payload = {}
        for k, v in request.form.items():
            payload[k] = cls.deserialize(v)
        for k, v in request.files.items():
            payload[k] = v.read()
        return listify(unflatten(payload))

@app.route('/data_sources', methods=['POST'])
def create_data_source():
    _, uid = Context.create(FlatDataSource)
    return json.dumps(uid)

@app.route('/annotations', methods=['POST'])
def create_annotation():
    _, uid = Context.create(Annotation)
    return json.dumps(uid)

@app.route('/feature_extractors/<class_name>', methods=['POST'])
def create_feature_extractor(class_name:str):
    extractor_class = app.config['FEATURE_EXTRACTOR_MAP'][class_name.title().replace('_', '')]
    _, uid = Context.create(extractor_class)
    return json.dumps(uid)

@app.route('/pixel_classifier', methods=['POST'])
def create_classifier():
    _, uid = Context.create(PixelClassifier)
    return json.dumps(uid)

@app.route('/predictions/')
def predict():
    pass

Thread(target=app.run).start()
