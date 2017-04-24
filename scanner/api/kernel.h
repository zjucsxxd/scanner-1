/* Copyright 2016 Carnegie Mellon University
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include "scanner/api/frame.h"
#include "scanner/util/common.h"
#include "scanner/util/memory.h"
#include "scanner/util/profiler.h"

#include <vector>

namespace scanner {

//! Element in a Scanner table, byte buffer of arbitrary size.
struct Element {
  inline Frame* as_frame() { return reinterpret_cast<Frame*>(buffer); }
  inline const Frame* as_const_frame() const {
    return reinterpret_cast<Frame*>(buffer);
  }
  inline FrameInfo* as_frame_info() {
    return reinterpret_cast<FrameInfo*>(buffer);
  }
  inline const FrameInfo* as_const_frame_info() const {
    return reinterpret_cast<FrameInfo*>(buffer);
  }

  Element() = default;
  Element(const Element&) = default;
  Element(Element&&) = default;
  Element& operator=(const Element&) = default;

  Element(u8* buffer, size_t size);
  Element(Frame* frame);

  u8* buffer;
  size_t size;
  bool is_frame;
};

using ElementList = std::vector<Element>;

using BatchedColumns = std::vector<ElementList>;

using StenciledColumns = std::vector<ElementList>;

//! Column -> Batch -> Stencil
using StenciledBatchedColumns = std::vector<std::vector<ElementList>>;

using Columns = std::vector<Element>;

inline size_t num_rows(const ElementList& column) { return column.size(); }

inline void insert_element(ElementList& column, u8* buffer, size_t size) {
  column.push_back(::scanner::Element{buffer, size});
}

inline void insert_frame(ElementList& column, Frame* frame) {
  column.push_back(::scanner::Element{frame});
}

inline void insert_element(Element& element, u8* buffer, size_t size) {
  element = ::scanner::Element{buffer, size};
}

inline void insert_frame(Element& element, Frame* frame) {
  element = ::scanner::Element{frame};
}

inline void delete_element(DeviceHandle device, Element& element) {
  if (element.is_frame) {
    Frame* frame = element.as_frame();
    delete_buffer(device, frame->data);
    delete frame;
  } else {
    delete_buffer(device, element.buffer);
  }
}

//! Kernel parameters provided at instantiation.
struct KernelConfig {
  std::vector<DeviceHandle> devices;  //! Non-empty set of devices provided to
                                      //! the kernel.
  std::vector<std::string> input_columns;
  std::vector<std::string> output_columns;
  std::vector<u8> args;  //! Byte-string of proto args if given.
  i32 work_item_size;
  i32 node_id;
  i32 node_count;
};

/**
 * @brief Interface for a unit of computation in a pipeline.
 *
 * Kernels form the core of Scanner's interface. They are essentially
 * functions that take elements of inputs and produce an equal number elements
 * of
 * outputs. Kernels are stateful operators that get reset when provided
 * non-contiguous batches of input. See KernelFactory for how an op
 * defines what hardware it can use for its computation.
 */
class BaseKernel {
 public:
  static const i32 UnlimitedDevices = 0;
  BaseKernel(const KernelConfig& config);

  virtual ~BaseKernel(){};

  /**
   * @brief Checks if kernel arguments are valid.
   *
   * Only useful if your kernel has its own custom Protobuf arguments.
   */
  virtual void validate(proto::Result* result) { result->set_success(true); }

  /**
   * @brief Resets ops when about to receive non-consecutive inputs.
   *
   * Scanner tries to run ops on consecutive blocks of inputs to
   * maximize the accuracy of stateful algorithms like video trackers.
   * However, when the runtime provides an op with a non-consecutive
   * input (because of work imbalance or other reasons), it will call reset
   * to allow the op to reset its state.
   */
  virtual void reset(){};

  /**
   * @brief For internal use
   **/
  virtual void execute_kernel(const StenciledBatchedColumns& input_columns,
                              BatchedColumns& output_columns) = 0;

  //! Do not call this function.
  virtual void set_profiler(Profiler* profiler) { profiler_ = profiler; }

  /**
   * The profiler allows an op to save profiling data for later
   * visualization. It is not guaranteed to be non-null, so check before use.
   */
  Profiler* profiler_ = nullptr;
};


/**
 * @brief Interface for a unit of computation in a pipeline.
 *
 * Kernels form the core of Scanner's interface. They are essentially
 * functions that take elements of inputs and produce an equal number elements
 * of
 * outputs. Kernels are stateful operators that get reset when provided
 * non-contiguous batches of input. See KernelFactory for how an op
 * defines what hardware it can use for its computation.
 */
class StenciledBatchedKernel : public BaseKernel {
 public:
  static const i32 UnlimitedDevices = 0;
  StenciledBatchedKernel(const KernelConfig& config);

  virtual ~StenciledBatchedKernel(){};

  /**
   * @brief Checks if kernel arguments are valid.
   *
   * Only useful if your kernel has its own custom Protobuf arguments.
   */
  virtual void validate(proto::Result* result) { result->set_success(true); }

  /**
   * @brief Resets ops when about to receive non-consecutive inputs.
   *
   * Scanner tries to run ops on consecutive blocks of inputs to
   * maximize the accuracy of stateful algorithms like video trackers.
   * However, when the runtime provides an op with a non-consecutive
   * input (because of work imbalance or other reasons), it will call reset
   * to allow the op to reset its state.
   */
  virtual void reset(){};

  /**
   * @brief For internal use
   **/
  virtual void execute_kernel(const StenciledBatchedColumns& input_columns,
                              BatchedColumns& output_columns) override;

  //! Do not call this function.
  virtual void set_profiler(Profiler* profiler) { profiler_ = profiler; }

 protected:
  /**
   * @brief Runs the op on input elements and produces equal number of
   *        output elements.
   *
   * @param input_columns
   *        vector of columns, where each column is a vector of inputs and
   * each
   *        input is a byte array
   * @param output_columns
   *        op output, each column must have same length as the number of
   *        input elements
   *
   * Evaluate gets run on batches of inputs. At the beginning of a pipeline
   * this
   * is raw RGB images from the input images/videos, and after that the input
   * becomes whatever was returned by the previous op.
   *
   * Number of output columns must be non-zero.
   */
  virtual void execute(const StenciledBatchedColumns& input_columns,
                       BatchedColumns& output_columns) = 0;

  /**
   * The profiler allows an op to save profiling data for later
   * visualization. It is not guaranteed to be non-null, so check before use.
   */
  Profiler* profiler_ = nullptr;
};

/**
 * @brief Interface for a unit of computation in a pipeline.
 *
 * Kernels form the core of Scanner's interface. They are essentially
 * functions that take elements of inputs and produce an equal number elements
 * of
 * outputs. Kernels are stateful operators that get reset when provided
 * non-contiguous batches of input. See KernelFactory for how an op
 * defines what hardware it can use for its computation.
 */
class BatchedKernel : public BaseKernel {
 public:
  BatchedKernel(const KernelConfig& config);

  virtual ~BatchedKernel(){};

  /**
   * @brief For internal use
   **/
  virtual void execute_kernel(const StenciledBatchedColumns& input_columns,
                              BatchedColumns& output_columns);
 protected:
  /**
   * @brief Runs the op on input elements and produces equal number of
   *        output elements.
   *
   * @param input_columns
   *        vector of columns, where each column is a vector of inputs and
   * each
   *        input is a byte array
   * @param output_columns
   *        op output, each column must have same length as the number of
   *        input elements
   *
   * Evaluate gets run on batches of inputs. At the beginning of a pipeline
   * this
   * is raw RGB images from the input images/videos, and after that the input
   * becomes whatever was returned by the previous op.
   *
   * Number of output columns must be non-zero.
   */
  virtual void execute(const BatchedColumns& input_columns,
                       BatchedColumns& output_columns) = 0;
};

class StenciledKernel : public BaseKernel {
 public:
  StenciledKernel(const KernelConfig& config);

  virtual ~StenciledKernel(){};

  /**
   * @brief For internal use
   **/
  virtual void execute_kernel(const StenciledBatchedColumns& input_columns,
                              BatchedColumns& output_columns);
 protected:
  /**
   * @brief Runs the op on input elements and produces equal number of
   *        output elements.
   *
   * @param input_columns
   *        vector of columns, where each column is a vector of inputs and
   * each
   *        input is a byte array
   * @param output_columns
   *        op output, each column must have same length as the number of
   *        input elements
   *
   * Evaluate gets run on batches of inputs. At the beginning of a pipeline
   * this
   * is raw RGB images from the input images/videos, and after that the input
   * becomes whatever was returned by the previous op.
   *
   * Number of output columns must be non-zero.
   */
  virtual void execute(const StenciledColumns& input_columns,
                       Columns& output_columns) = 0;
};

class Kernel : public BaseKernel {
 public:
  Kernel(const KernelConfig& config);

  virtual ~Kernel(){};

  /**
   * @brief For internal use
   **/
  virtual void execute_kernel(const StenciledBatchedColumns& input_columns,
                              BatchedColumns& output_columns);
 protected:
  /**
   * @brief Runs the op on input elements and produces equal number of
   *        output elements.
   *
   * @param input_columns
   *        vector of elements, where each element is from a different column
   * @param output_columns
   *        op output, vector of elements, where each element is from a
   *        different column
   *
   * Evaluate gets run on batches of inputs. At the beginning of a pipeline
   * this
   * is raw RGB images from the input images/videos, and after that the input
   * becomes whatever was returned by the previous op.
   *
   * Number of output columns must be non-zero.
   */
  virtual void execute(const Columns& input_columns,
                       Columns& output_columns) = 0;
};

//! Kernel with support for frame and frame_info columns.
class VideoKernel {
 protected:
  /**
   * @brief Checks frame info column against cached data.
   *
   * This function should be called at the top of the execute function on the
   * frame info column. If the frame info changes, e.g. the kernel is processing
   * a new video, then this calls new_frame_info which you can override.
   */
  void check_frame(const DeviceHandle& device, const Element& element);

  void check_frame_info(const DeviceHandle& device, const Element& element);

  //! Callback for if frame info changes.
  virtual void new_frame_info(){};

  FrameInfo frame_info_{};
};

///////////////////////////////////////////////////////////////////////////////
/// Implementation Details
namespace internal {

class KernelBuilder;

using KernelConstructor =
    std::function<BaseKernel*(const KernelConfig& config)>;

class KernelRegistration {
 public:
  KernelRegistration(const KernelBuilder& builder);
};

class KernelBuilder {
 public:
  friend class KernelRegistration;

  KernelBuilder(const std::string& name, KernelConstructor constructor)
    : name_(name),
      constructor_(constructor),
      device_type_(DeviceType::CPU),
      num_devices_(1),
      preferred_batch_size_(1) {}

  KernelBuilder& device(DeviceType device_type) {
    device_type_ = device_type;
    return *this;
  }

  KernelBuilder& num_devices(i32 devices) {
    num_devices_ = devices;
    return *this;
  }

  KernelBuilder& batch(i32 preferred_batch_size = 1) {
    preferred_batch_size = preferred_batch_size;
    return *this;
  }

 private:
  std::string name_;
  KernelConstructor constructor_;
  DeviceType device_type_;
  i32 num_devices_;
  i32 preferred_batch_size_;
};
}

#define REGISTER_KERNEL(name__, kernel__) \
  REGISTER_KERNEL_HELPER(__COUNTER__, name__, kernel__)

#define REGISTER_KERNEL_HELPER(uid__, name__, kernel__) \
  REGISTER_KERNEL_UID(uid__, name__, kernel__)

#define REGISTER_KERNEL_UID(uid__, name__, kernel__)                         \
  static ::scanner::internal::KernelRegistration kernel_registration_##uid__ \
      __attribute__((unused)) = ::scanner::internal::KernelBuilder(          \
          #name__, [](const ::scanner::KernelConfig& config) {             \
            return new kernel__(config);                                     \
          })
}
