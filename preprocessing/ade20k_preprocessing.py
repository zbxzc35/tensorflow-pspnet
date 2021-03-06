from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from tensorflow.python.ops import control_flow_ops

slim = tf.contrib.slim

_R_MEAN = 123.68
_G_MEAN = 116.78
_B_MEAN = 103.94

_RESIZE_SIDE_MIN = 256
_RESIZE_SIDE_MAX = 512


def _crop(image, offset_height, offset_width, crop_height, crop_width):
  original_shape = tf.shape(image)

  rank_assertion = tf.Assert(
      tf.equal(tf.rank(image), 3),
      ['Rank of image must be equal to 3.'])
  cropped_shape = control_flow_ops.with_dependencies(
      [rank_assertion],
      tf.pack([crop_height, crop_width, original_shape[2]]))

  size_assertion = tf.Assert(
      tf.logical_and(
          tf.greater_equal(original_shape[0], crop_height),
          tf.greater_equal(original_shape[1], crop_width)),
      ['Crop size greater than the image size.'])

  offsets = tf.to_int32(tf.pack([offset_height, offset_width, 0]))

  # Use tf.slice instead of crop_to_bounding box as it accepts tensors to
  # define the crop size.
  image = control_flow_ops.with_dependencies(
      [size_assertion],
      tf.slice(image, offsets, cropped_shape))
  return tf.reshape(image, cropped_shape)


def _random_crop(image_list, label_list, crop_height, crop_width):
  if not image_list:
    raise ValueError('Empty image_list.')

  # Compute the rank assertions.
  rank_assertions = []
  for i in range(len(image_list)):
    image_rank = tf.rank(image_list[i])
    rank_assert = tf.Assert(
        tf.equal(image_rank, 3),
        ['Wrong rank for tensor  %s [expected] [actual]',
         image_list[i].name, 3, image_rank])
    rank_assertions.append(rank_assert)

  image_shape = control_flow_ops.with_dependencies(
      [rank_assertions[0]],
      tf.shape(image_list[0]))
  image_height = image_shape[0]
  image_width = image_shape[1]
  crop_size_assert = tf.Assert(
      tf.logical_and(
          tf.greater_equal(image_height, crop_height),
          tf.greater_equal(image_width, crop_width)),
      ['Crop size greater than the image size.', image_height, image_width, crop_height, crop_width])

  asserts = [rank_assertions[0], crop_size_assert]

  for i in range(1, len(image_list)):
    image = image_list[i]
    asserts.append(rank_assertions[i])
    shape = control_flow_ops.with_dependencies([rank_assertions[i]],
                                               tf.shape(image))
    height = shape[0]
    width = shape[1]

    height_assert = tf.Assert(
        tf.equal(height, image_height),
        ['Wrong height for tensor %s [expected][actual]',
         image.name, height, image_height])
    width_assert = tf.Assert(
        tf.equal(width, image_width),
        ['Wrong width for tensor %s [expected][actual]',
         image.name, width, image_width])
    asserts.extend([height_assert, width_assert])

  # Create a random bounding box.
  #
  # Use tf.random_uniform and not numpy.random.rand as doing the former would
  # generate random numbers at graph eval time, unlike the latter which
  # generates random numbers at graph definition time.
  max_offset_height = control_flow_ops.with_dependencies(
      asserts, tf.reshape(image_height - crop_height + 1, []))
  max_offset_width = control_flow_ops.with_dependencies(
      asserts, tf.reshape(image_width - crop_width + 1, []))
  offset_height = tf.random_uniform(
      [], maxval=max_offset_height, dtype=tf.int32)
  offset_width = tf.random_uniform(
      [], maxval=max_offset_width, dtype=tf.int32)

  cropped_images = [_crop(image, offset_height, offset_width,
                          crop_height, crop_width) for image in image_list]
  cropped_labels = [_crop(label, offset_height, offset_width,
                          crop_height, crop_width) for label in label_list]
  return cropped_images, cropped_labels


def _central_crop(image_list, label_list, crop_height, crop_width):
  output_images = []
  output_labels = []
  for image, label in zip(image_list, label_list):
    image_height = tf.shape(image)[0]
    image_width = tf.shape(image)[1]

    offset_height = (image_height - crop_height) / 2
    offset_width = (image_width - crop_width) / 2

    output_images.append(_crop(image, offset_height, offset_width,
                               crop_height, crop_width))
    output_labels.append(_crop(label, offset_height, offset_width,
                               crop_height, crop_width))
  return output_images, output_labels


def _mean_image_subtraction(image, means):
  if image.get_shape().ndims != 3:
    raise ValueError('Input must be of size [height, width, C>0]')
  num_channels = image.get_shape().as_list()[-1]
  if len(means) != num_channels:
    raise ValueError('len(means) must match the number of channels')

  channels = tf.split(2, num_channels, image)
  for i in range(num_channels):
    channels[i] -= means[i]
  return tf.concat(2, channels)


def _smallest_size_at_least(height, width, smallest_side):
  smallest_side = tf.convert_to_tensor(smallest_side, dtype=tf.int32)

  height = tf.to_float(height)
  width = tf.to_float(width)
  smallest_side = tf.to_float(smallest_side)

  scale = tf.cond(tf.greater(height, width),
                  lambda: smallest_side / width,
                  lambda: smallest_side / height)
  new_height = tf.to_int32(height * scale)
  new_width = tf.to_int32(width * scale)
  return new_height, new_width


def _aspect_preserving_resize(image, label, smallest_side):
  smallest_side = tf.convert_to_tensor(smallest_side, dtype=tf.int32)

  shape = tf.shape(image)
  height = shape[0]
  width = shape[1]
  new_height, new_width = _smallest_size_at_least(height, width, smallest_side)

  image = tf.expand_dims(image, 0)
  resized_image = tf.image.resize_bilinear(image, [new_height, new_width],
                                           align_corners=False)
  resized_image = tf.squeeze(resized_image, axis=[0])
  resized_image.set_shape([None, None, 3])

  label = tf.expand_dims(label, 0)
  resized_label = tf.image.resize_nearest_neighbor(label, [new_height, new_width],
                                                   align_corners=False)
  resized_label = tf.squeeze(resized_label, axis=[0])
  resized_label.set_shape([None, None, 1])
  return resized_image, resized_label


def preprocess_for_train(image,
                         label,
                         output_height,
                         output_width,
                         resize_side_min=_RESIZE_SIDE_MIN,
                         resize_side_max=_RESIZE_SIDE_MAX):
  resize_side = tf.random_uniform(
      [], minval=resize_side_min, maxval=resize_side_max+1, dtype=tf.int32)

  image, label = _aspect_preserving_resize(image, label, resize_side)
  cropped_images, cropped_labels = _random_crop([image], [label],
                                                output_height, output_width)
  image, label = cropped_images[0], cropped_labels[0]
  image.set_shape([output_height, output_width, 3])
  label.set_shape([output_height, output_width, 1])
  image = tf.to_float(image)
  label = tf.to_int32(label)

  val_lr = tf.to_float(tf.random_uniform([1]))[0]
  image = tf.cond(val_lr > 0.5, lambda: tf.image.flip_left_right(image), lambda: image)
  label = tf.cond(val_lr > 0.5, lambda: tf.image.flip_left_right(label), lambda: label)

  val_ud = tf.to_float(tf.random_uniform([1]))[0]
  image = tf.cond(val_ud > 0.5, lambda: tf.image.flip_up_down(image), lambda: image)
  label = tf.cond(val_ud > 0.5, lambda: tf.image.flip_up_down(label), lambda: label)

  return _mean_image_subtraction(image, [_R_MEAN, _G_MEAN, _B_MEAN]), label


def preprocess_for_eval(image, label, output_height, output_width, resize_side):
  image, label = _aspect_preserving_resize(image, label, resize_side)
  cropped_images, cropped_labels = _central_crop([image], [label], output_height, output_width)
  image = cropped_images[0]
  label = cropped_labels[0]

  image.set_shape([output_height, output_width, 3])
  label.set_shape([output_height, output_width, 1])

  image = tf.to_float(image)
  label = tf.to_int32(label)

  return _mean_image_subtraction(image, [_R_MEAN, _G_MEAN, _B_MEAN]), label


def preprocess_image(image, output_height, output_width,
                     label=None,
                     is_training=False,
                     resize_side_min=_RESIZE_SIDE_MIN,
                     resize_side_max=_RESIZE_SIDE_MAX):
  if is_training:
    return preprocess_for_train(image, label, output_height, output_width,
                                resize_side_min, resize_side_max)
  else:
    return preprocess_for_eval(image, label, output_height, output_width,
                               resize_side_min)
