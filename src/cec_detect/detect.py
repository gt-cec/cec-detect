# detect.py: conducts object detection to get the bounding boxes of relevant objects in the scene

import torch
import numpy as np
from transformers import Owlv2Processor, Owlv2ForObjectDetection
import utils
import sys
import os

class Detector():
    def __init__(self):
        # set device to cuda if cuda is available
        if torch.cuda.is_available():
            self.device = "cuda"
            print("Using CUDA")
        # otherwise check if on macos
        elif sys.platform == "darwin":
            self.device = "mps"
            print("Using MPS")
        else:
            self.device = "cpu"
            print("Using CPU")

        # if you get an undefined symbol:ffi_type_uint32, version LIBFFI_BASE_7.0 error, set the env var LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libffi.so.7
        self.processor = Owlv2Processor.from_pretrained("google/owlv2-base-patch16-ensemble")
        self.model = Owlv2ForObjectDetection.from_pretrained("google/owlv2-base-patch16-ensemble").to(torch.device(self.device))

        # load the segmentation model
        try:
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor

            # adding SAM2 config path from here: https://github.com/facebookresearch/sam2/issues/81#issuecomment-2262979343
            # import hydra
            # hydra is initialized on import of sam2, which sets the search path which can't be modified
            # so we need to clear the hydra instance
            # hydra.core.global_hydra.GlobalHydra.instance().clear()
            # reinit hydra with a new search path for configs
            # hydra.initialize_config_module('', version_base='1.2')/Users/jack/Documents/Installations/sam2

            # this should work now
            # model = build_sam2('<config-name>', '<checkpoint-path>')

            sam2_checkpoint = os.path.dirname(os.path.abspath(__file__)) + "/sam2.1_hiera_large.pt"
            model_cfg = "configs/sam2.1/sam2.1_hiera_l.yaml"
            sam2_model = build_sam2(model_cfg, sam2_checkpoint, device=self.device)
            self.predictor = SAM2ImagePredictor(sam2_model)
        except FileNotFoundError as e:
            print("[CEC Detect] WARNING: Could not find SAM2 weights or config, place sam2.1_hiera_large.pt in the cec_detect/src folder, and sam2.1_hiera_l.yaml in cec_detect/configs/sam2.1/")
        except ModuleNotFoundError as e:
            print("[CEC Detect] WARNING: SAM2 is not installed, do not expect to use it. Message:", e)
            self.predictor = None

    # main detection function
    def detect(self, image, classes, threshold=0.1):
        texts = [["" + c for c in classes]]
        inputs = self.processor(text=texts, images=image, return_tensors="pt").to(torch.device(self.device))
        with torch.no_grad():
            outputs = self.model(**inputs)

        h, w = inputs.pixel_values.shape[-2:]

        # Convert outputs (bounding boxes and class logits) to COCO API format
        results = self.processor.post_process_object_detection(
            outputs=outputs, target_sizes=[(h, w)], threshold=threshold
        )[0]  # we only pass one image in, so can take the first result [[results]]

        boxes, scores, labels = results["boxes"], results["scores"], results["labels"]

        clip_to_orig_width, clip_to_orig_height = image.shape[1] / w, image.shape[0] / (h * (image.shape[0] / image.shape[1]))
        objects = []
        object_idx_by_class_id = {}  # for checking overlapping boxes
        class_list = list(classes)
        for box, score, label, i in zip(boxes, scores, labels, range(len(boxes))):
            box = [round(i, 2) for i in box.tolist()]
            box[0] *= clip_to_orig_width
            box[1] *= clip_to_orig_height
            box[2] *= clip_to_orig_width
            box[3] *= clip_to_orig_height
            box = [[int(box[0]), int(box[1])], [int(box[2]), int(box[3])]]
            label = int(label)
            objects.append({
                "class": class_list[label],
                "class id": label,
                "confidence": round(score.item(), 3),
                "box": box,  # [[x1,y1], [x2,y2]] from top left, NOT [[row1,col1],[row2,col2]]
                "center": [(box[0][0] + box[1][0]) / 2, (box[0][1] + box[1][1]) / 2]
            })
            if label not in object_idx_by_class_id:
                object_idx_by_class_id[label] = [i]
            else:
                object_idx_by_class_id[label].append(i)
            print(f"Detected {texts[0][label]} {label} with confidence {round(score.item(), 3)} at location {box}")

        # remove objects that significantly overlap by choosing highest
        overlap_threshold = 0.9
        for class_id in object_idx_by_class_id:
            for object_idx_1 in range(len(object_idx_by_class_id[class_id])):
                for object_idx_2 in range(object_idx_1, len(object_idx_by_class_id[class_id])):
                    # skip if same index or we have already thrown out one of the objects
                    if object_idx_1 == object_idx_2 or objects[object_idx_by_class_id[class_id][object_idx_2]] is None or objects[object_idx_by_class_id[class_id][object_idx_1]] is None:
                        continue
                    if self.__calculate_overlap_proportion__(objects[object_idx_by_class_id[class_id][object_idx_1]]["box"], objects[object_idx_by_class_id[class_id][object_idx_2]]["box"]) > overlap_threshold:
                        if objects[object_idx_by_class_id[class_id][object_idx_1]]["confidence"] > objects[object_idx_by_class_id[class_id][object_idx_2]]["confidence"]:
                            objects[object_idx_by_class_id[class_id][object_idx_2]] = None
                        else:
                            objects[object_idx_by_class_id[class_id][object_idx_1]] = None
        objects = [x for x in objects if x is not None]  # remove the objects we filtered out

        return objects
    
    # given a bounding box, get the segmentation
    # inputs: RGB image, detected bounding boxes
    # outputs: segmentation masks (should be 1 per bounding box)
    def segment(self, rgb, boxes):
        """
        Given an RGB image and a list of bounding boxes, run a model to segment the bounding boxes. Uses SAM2.

        Inputs:
            rgb: Image in typical OpenCV RGB format: numpy array (H,W,3)
            boxes: List of N bounding boxes: [[[col1, row1], [col2, row2]], ... ]

        Output:
            masks: segmentation masks for each of the N boxes stacked: numpy array (N,H,W)
        """
        # edge case: no boxes, return with no masks
        if len(boxes) == 0:
            return np.array([])
        # check if predictor is None (mean's SAM2 is not installed)
        assert self.predictor is not None, "The segmentation system is being called, however SAM2 is not installed. Install SAM2 first."

        self.predictor.set_image(rgb)  # give the image to the predictor
        masks, scores, _ = self.predictor.predict(
            point_coords=None,
            point_labels=None,
            box=boxes,
            multimask_output=False,
        )  # predict the segmentations using the bounding boxes
        masks = masks.reshape((masks.shape[0], rgb.shape[0], rgb.shape[1]))  # N x 1 x 512 x 512 to N x 512 x 512
        return masks == 1  # convert from 0's and 1's to False and True

    # removes boxes from detected object set A (main set) that are not in detected object set B (check set)
    # this is used for syncing RGB and depth detected objects
    def remove_objects_not_overlapping(self, objects_main, objects_check, overlap_threshold=0.8, classes_to_filter=None):
        classes = {}  # organize by class
        for i, o in enumerate(objects_main):  # set up classes for the main objects
            if classes_to_filter is None or o["class"] not in classes_to_filter:  # if classes to filter were specified, don't count classes that were not specified
                continue
            if o["class"] not in classes:
                classes[o["class"]] = [[], []]  # 0th index: main, 1st index: check
            classes[o["class"]][0].append(i)  # add the object to this class
        for i, o in enumerate(objects_check):  # set up classes for the check objects
            if o["class"] not in classes:  # ignore check objects not in the main objects
                continue
            classes[o["class"]][1].append(i)  # add the object to this class
        main_objects_to_remove = []
        for c in classes:  # remove overlapping
            for obj_main_idx in classes[c][0]:  # for each main object in a class
                closest_match_check_classes_idx = None
                closest_match_check_overlap = 0
                for classes_check_idx, obj_check_idx in enumerate(classes[c][1]):  # for each check object of that class
                    overlap = self.__calculate_overlap_proportion__(objects_main[obj_main_idx]["box"], objects_check[obj_check_idx]["box"])
                    if overlap >= overlap_threshold and overlap > closest_match_check_overlap:  # check if the overlap passes the threshold and is more than other overlaps
                        closest_match_check_classes_idx = classes_check_idx
                        closest_match_check_overlap = overlap
                if closest_match_check_classes_idx is None:  # if main object has no check objects, it is not overlapping anything, so delete the main object
                    main_objects_to_remove.append(obj_main_idx)
                else:  # otherwise, there is a close match to a check object, so keep the main object and delete the check object from the classes
                    del classes[c][1][closest_match_check_classes_idx]
        return [x for i, x in enumerate(objects_main) if i not in main_objects_to_remove]  # return the updated objects main


    # gets the percent overlap of two boxes, generated with Claude
    def __calculate_overlap_proportion__(self, box1, box2):
        # unpack the coordinates
        ((x1_1, y1_1), (x2_1, y2_1)) = box1
        ((x1_2, y1_2), (x2_2, y2_2)) = box2

        # calculate the coordinates of the intersection rectangle
        x_left = max(x1_1, x1_2)
        y_top = max(y1_1, y1_2)
        x_right = min(x2_1, x2_2)
        y_bottom = min(y2_1, y2_2)

        if x_right < x_left or y_bottom < y_top:  # check if there is an overlap
            return 0.0

        intersection_area = (x_right - x_left) * (y_bottom - y_top)  # calculate the area of intersection

        # calculate the area of both boxes
        box1_area = (x2_1 - x1_1) * (y2_1 - y1_1)
        box2_area = (x2_2 - x1_2) * (y2_2 - y1_2)

        union_area = box1_area + box2_area - intersection_area  # calculate the union area
        overlap_proportion = intersection_area / union_area  # calculate the overlap proportion
        return overlap_proportion
