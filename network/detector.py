import tensorflow as tf
import numpy as np
import cv2
import network.config as cfg
from utils.timer import step_time
import os 

class Detector(object):

    def __init__(self, net, weight_file, save_file):
        self.net = net
        self.weights_file = weight_file
        
        self.save           = save_file
        self.classes        = cfg.CLASSES
        self.colors         = cfg.COLORS
        self.num_class      = len(self.classes)
        self.image_size     = cfg.IMAGE_SIZE
        self.cell_size      = cfg.CELL_SIZE
        self.boxes_per_cell = cfg.BOXES_PER_CELL
        self.threshold      = cfg.THRESHOLD
        self.iou_threshold  = cfg.IOU_THRESHOLD
        self.boundary1      = self.cell_size ** 2 * self.num_class
        self.boundary2      = self.cell_size ** 2 * self.boxes_per_cell  + self.boundary1


        self.sess = tf.Session()
        self.sess.run(tf.global_variables_initializer())

        print ('Restoring weights from: ' + self.weights_file)
        self.saver = tf.train.Saver()
        self.saver.restore(self.sess, self.weights_file)

    def draw_prediction(self, img, result):
        for i in range(len(result)):
            x, y = int(result[i][1]),   int(result[i][2])
            w, h = int(result[i][3]/2), int(result[i][4]/2)
            #bouding box
            cv2.rectangle(img, (x - w, y - h), (x + w, y + h), self.colors[result[i][0]], 2)
            
            #text background box
            cv2.rectangle(img, (x - w, y - h - 20),(x + w, y - h),  self.colors[result[i][0]], -1)
            
            #class
            cv2.putText(img, self.classes[result[i][0]] + ' : %.2f' % result[i][5],
                        (x - w + 5, y - h - 7), cv2.FONT_HERSHEY_SIMPLEX, 
                        0.5, (255,255,255), 1, cv2.LINE_AA)

    @step_time('Detection')
    def detect(self, img):
        img_h, img_w, _ = img.shape
        inputs = cv2.resize(img, (self.image_size, self.image_size))
        inputs = cv2.cvtColor(inputs, cv2.COLOR_BGR2RGB).astype(np.float32) # because opencv is weird
        inputs = (inputs / 255.0) * 2.0 - 1.0 #normalization
        inputs = np.reshape(inputs, (1, self.image_size, self.image_size, 3))#make a 4D tensor (batch_size = 1)

        result = self.inference(inputs)

        for i in range(len(result)):
            result[i][1] *= img_w / self.image_size
            result[i][2] *= img_h / self.image_size
            result[i][3] *= img_w / self.image_size
            result[i][4] *= img_h / self.image_size

        return result

    def inference(self, inputs):
        net_output = self.sess.run(self.net.logits, feed_dict={self.net.images: inputs})
        
        results = []
        for i in range(net_output.shape[0]):
            results.append(self.interpret(net_output[i]))
        return results[0]

    def interpret(self, output):
        
        probs = np.zeros((self.cell_size,
                          self.cell_size,
                          self.boxes_per_cell,
                          self.num_class))
        
        # P(class|object is present)
        class_probs = np.reshape(output[0:self.boundary1], (self.cell_size, \
                                                            self.cell_size, \
                                                            self.num_class))
        
        # confidence scrore of detected boxes = P(object is present)
        scales = np.reshape(output[self.boundary1:self.boundary2], (self.cell_size, \
                                                                    self.cell_size, \
                                                                    self.boxes_per_cell))
        # (x,y,sqrt(w),sqrt(h)) of the detected boxes
        boxes = np.reshape(output[self.boundary2:], (self.cell_size,\
                                                     self.cell_size,\
                                                     self.boxes_per_cell, \
                                                     4))
        offset = np.transpose(np.reshape(np.array([np.arange(self.cell_size)] * self.cell_size * self.boxes_per_cell),
                                         [self.boxes_per_cell, self.cell_size, self.cell_size]), (1, 2, 0))

        boxes[:, :, :, 0] += offset
        boxes[:, :, :, 1] += np.transpose(offset, (1, 0, 2))
        boxes[:, :, :, :2] = boxes[:, :, :, 0:2] / self.cell_size
        boxes[:, :, :, 2:] = np.square(boxes[:, :, :, 2:])

        boxes *= self.image_size

        #compute likelyhood of each class as each cell
        for i in range(self.boxes_per_cell):
            for j in range(self.num_class):
                # p(class) = P(class|object is present)P(object is present)
                probs[:, :, i, j] = np.multiply(class_probs[:, :, j], scales[:, :, i])

        filter_mat_probs = np.array(probs >= self.threshold, dtype='bool')
        filter_mat_boxes = np.nonzero(filter_mat_probs) #indices
         
        #box that heve at least one likely class
        boxes_filtered = boxes[filter_mat_boxes[0],
                               filter_mat_boxes[1],
                               filter_mat_boxes[2]]
        
        #probs above the threshold
        probs_filtered = probs[filter_mat_probs]
        
        #find the most likely class for each cell
        classes_num_filtered = np.argmax(filter_mat_probs, axis=3)[filter_mat_boxes[0], \
                                                                   filter_mat_boxes[1], \
                                                                   filter_mat_boxes[2]]
        #sort the  from most likely to less likely
        argsort = np.array(np.argsort(probs_filtered))[::-1]
        boxes_filtered = boxes_filtered[argsort]
        probs_filtered = probs_filtered[argsort]
        classes_num_filtered = classes_num_filtered[argsort]
        
        #filter overlaping boxes
        for i in range(len(boxes_filtered)):
            if probs_filtered[i] == 0:
                continue
            for j in range(i + 1, len(boxes_filtered)):
                #if two boxes are over each other, the least likely of the two is destroyed
                if self.iou(boxes_filtered[i], boxes_filtered[j]) > self.iou_threshold:
                    probs_filtered[j] = 0.0

        filter_iou = np.array(probs_filtered > 0.0, dtype='bool')
        boxes_filtered = boxes_filtered[filter_iou]
        probs_filtered = probs_filtered[filter_iou]
        classes_num_filtered = classes_num_filtered[filter_iou]

        result = []
        for i in range(len(boxes_filtered)):
            result.append([classes_num_filtered[i],
                           boxes_filtered[i][0], boxes_filtered[i][1],
                           boxes_filtered[i][2], boxes_filtered[i][3], 
                           probs_filtered[i]])

        return result

    def iou(self, box1, box2):
        '''
        intersection over union
        '''
        ud = min(box1[0] + box1[2] / 2, box2[0] + box2[2] / 2) - \
             max(box1[0] - box1[2] / 2, box2[0] - box2[2] / 2)
        lr = min(box1[1] + box1[3] / 2, box2[1] + box2[3] / 2) - \
             max(box1[1] - box1[3] / 2, box2[1] - box2[3] / 2)
        
        intersection = 0 if (ud < 0 or lr < 0) else ud * lr
        
        return intersection / (box1[2] * box1[3] + box2[2] * box2[3] - intersection)
    
    def __call__(self, input_name, wait = 10):
        
        #not the most beautiful code, must come back to correct it!
        if type(input_name) == str:           
            image = cv2.imread(input_name)
            
            result = self.detect(image)          
            self.draw_prediction(image, result)
            cv2.imshow('Image : ' + input_name, image)
            cv2.waitKey(0)
            if self.save:
                if not os.path.exists('results'):
                    os.makedirs('results')
                cv2.imwrite(os.path.join('results',os.path.basename(input_name)),image)
            
        else:
            print('video')
            ret, _ = input_name.read()
            print(ret)
            while ret: 
                ret, frame = input_name.read()
                
                result = self.detect(frame)        
                self.draw_prediction(frame, result)
                cv2.imshow('Camera', frame)
                cv2.waitKey(wait)
        
                ret, frame = input_name.read()