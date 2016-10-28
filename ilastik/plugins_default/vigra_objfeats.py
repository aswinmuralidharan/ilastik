###############################################################################
#   ilastik: interactive learning and segmentation toolkit
#
#       Copyright (C) 2011-2014, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# In addition, as a special exception, the copyright holders of
# ilastik give you permission to combine ilastik with applets,
# workflows and plugins which are not covered under the GNU
# General Public License.
#
# See the LICENSE file for details. License information is also available
# on the ilastik web site at:
#		   http://ilastik.org/license.html
###############################################################################
from ilastik.plugins import ObjectFeaturesPlugin
import ilastik.applets.objectExtraction.opObjectExtraction
#from ilastik.applets.objectExtraction.opObjectExtraction import make_bboxes, max_margin
import vigra
import numpy as np
from lazyflow.request import Request, RequestPool

def cleanup_key(k):
    return k.replace(' ', '')

def cleanup_value(val, nObjects, isGlobal):
    """ensure that the value is a numpy array with the correct shape."""
    val = np.asarray(val)

    if val.ndim == 0 or isGlobal:
        # repeat the global values for all the objects 
        scalar = val.reshape((1,))[0]
        val = np.zeros((nObjects, 1), dtype=val.dtype)
        val[:, 0] = scalar
    
    if val.ndim == 1:
        val = val.reshape(-1, 1)
   
    if val.ndim > 2:
        val = val.reshape(val.shape[0], -1)

    assert val.shape[0] == nObjects
    # remove background
    val = val[1:]
    return val

def cleanup(d, nObjects, features):
    result = dict((cleanup_key(k), cleanup_value(v, nObjects, "Global" in k)) for k, v in d.iteritems())
    newkeys = set(result.keys()) & set(features)
    return dict((k, result[k]) for k in newkeys)

class VigraObjFeats(ObjectFeaturesPlugin):
    # features not in this list are assumed to be local.
    local_features = set(["Mean", "Variance", "Skewness", \
                          "Kurtosis", "Histogram", "Sum", \
                          "Covariance", "Minimum", "Maximum"])
    local_suffix = " in neighborhood" #note the space in front, it's important
    local_out_suffixes = [local_suffix, " in object and neighborhood"]

    ndim = None
    
    def availableFeatures(self, image, labels):
        names = vigra.analysis.supportedRegionFeatures(image, labels)
        names = list(f.replace(' ', '') for f in names)
        local = set(names) & self.local_features
        tooltips = {}
        names.extend([x+self.local_suffix for x in local])
        result = dict((n, {}) for n in names)  
        for f, v in result.iteritems():
            if self.local_suffix in f:
                v['margin'] = 0
            #build human readable names from vigra names
            #TODO: many cases are not covered
            props = self.find_properties(f)
            for prop_name, prop_value in props.iteritems():
                v[prop_name] = prop_value
        
        return result

    def find_properties(self, feature_name):

        tooltip = feature_name
        advanced = False
        displaytext = feature_name
        detailtext = feature_name

        if feature_name == "Count":
            displaytext = "Size in pixels"
            detailtext = "Size in pixels as we usually compute it. Just that. Nothing else."

        if "Central<PowerSum<" in feature_name:
            tooltip =  "Unnormalized central moment: Sum_i{(X_i-object_mean)^n}"
            advanced = True
        elif "PowerSum<" in feature_name:
            tooltip = "Unnormalized moment: Sum_i{(X_i)^n}"
            advanced = True
        elif "Minimum" in feature_name:
            tooltip = "Minimum"

        elif "Maximum" in feature_name:
            tooltip = "Maximum"
        elif "Variance" in feature_name:
            tooltip = "Variance"
        elif "Skewness" in feature_name:
            tooltip = "Skewness"
        elif "Kurtosis" in feature_name:
            tooltip = "Kurtosis"

        if "Principal<" in feature_name:
            tooltip = tooltip + ", projected onto PCA eigenvectors"
            advanced = True
        if "Coord<" in feature_name:
            tooltip = tooltip + ", computed from object pixel coordinates"
        if not "Coord<" in feature_name:
            tooltip = tooltip + ", computed from raw pixel values"
        if "DivideByCount<" in feature_name:
            tooltip = tooltip + ", divided by the number of pixels"
            advanced = True
        if self.local_suffix in feature_name:
            tooltip = tooltip + ", as defined by neighborhood size below"

        props = {}
        props["tooltip"] = tooltip
        props["advanced"] = advanced
        props["displaytext"] = displaytext
        props["detailtext"] = detailtext
        return props

    def _do_4d(self, image, labels, features, axes):
        if self.ndim==2:
            result = vigra.analysis.extractRegionFeatures(image.squeeze().astype(np.float32), labels.squeeze().astype(np.uint32), features, ignoreLabel=0)
        else:
            result = vigra.analysis.extractRegionFeatures(image.astype(np.float32), labels.astype(np.uint32), features, ignoreLabel=0)
            
        #take a non-global feature
        local_features = [x for x in features if "Global<" not in x]
        #find the number of objects
        nobj = result[local_features[0]].shape[0]
        
        #NOTE: this removes the background object!!!
        #The background object is always present (even if there is no 0 label) and is always removed here
        return cleanup(result, nobj, features)

    def compute_global(self, image, labels, features, axes):
        features = features.keys()
        local = [x+self.local_suffix for x in self.local_features]
        features = list(set(features) - set(local))
        
        #the image parameter passed here is the whole dataset. 
        #We can use it estimate if the data is 2D or 3D and then apply 
        #this knowledge in compute_local
        nZ = image.shape[axes.z]
        if nZ>1:
            self.ndim = 3
        else:
            self.ndim = 2
            
        return self._do_4d(image, labels, features, axes)

    def compute_local(self, image, binary_bbox, feature_dict, axes):
        """helper that deals with individual objects"""
        
        featurenames = feature_dict.keys()
        local = [x+self.local_suffix for x in self.local_features]
        featurenames = list(set(featurenames) & set(local))
        featurenames = [x.split(' ')[0] for x in featurenames]
        results = []
        margin = ilastik.applets.objectExtraction.opObjectExtraction.max_margin({'': feature_dict})
        #FIXME: this is done globally as if all the features have the same margin
        #we should group features by their margins
        passed, excl = ilastik.applets.objectExtraction.opObjectExtraction.make_bboxes(binary_bbox, margin)
        #assert np.all(passed==excl)==False
        #assert np.all(binary_bbox+excl==passed)
        for label, suffix in zip([excl, passed],
                                 self.local_out_suffixes):
            result = self._do_4d(image, label, featurenames, axes)
            results.append(self.update_keys(result, suffix=suffix))
        return self.combine_dicts(results)
