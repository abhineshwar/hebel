from pycuda import gpuarray
from pycuda.compiler import SourceModule
import numpy as np

CONV_BLOCK_SIZE = (32, 32, 1)
MAX_WIDTH_FILTER = 15
MAX_NUM_FILTERS = 100
assert CONV_BLOCK_SIZE[0] == CONV_BLOCK_SIZE[1]

code = """
#define CEILING(x) (int)(x) + (1 - (int)((int)((x) + 1) - (x)))

#define TILE_SIZE %(TILE_SIZE)d
#define MAX_WIDTH_FILTER %(MAX_WIDTH_FILTER)d
#define MAX_NUM_FILTERS %(MAX_NUM_FILTERS)d

__global__ void conv1d_matrix_mult_filter(const %(data_type)s *input,
    %(data_type)s *target, const %(data_type)s *filter,
    const unsigned int width, const unsigned int height,
    const unsigned int filter_width, const unsigned int n_filters,
    const unsigned int stride) {
    
    const unsigned int i = blockIdx.y*blockDim.y+threadIdx.y;
    const unsigned int j = blockIdx.x*blockDim.x+threadIdx.x;
    const unsigned int lin_idx = i*width+j;
    const unsigned int row_start = i*width;
    const unsigned int target_width = CEILING((double) width / stride);
    unsigned int shared_idx, input_idx;    
    
    const unsigned int shared_width = TILE_SIZE+MAX_WIDTH_FILTER-1;
    __shared__ %(data_type)s input_shared[TILE_SIZE*(shared_width)];
    
    const unsigned int halo_width = filter_width / 2;
    
    if (i < height) {
        int halo_index_left = (blockIdx.x-1)*blockDim.x+threadIdx.x;
        if (threadIdx.x >= blockDim.x-halo_width) {
            shared_idx = threadIdx.y*shared_width + 
                threadIdx.x-(blockDim.x-halo_width);
            input_idx = row_start + halo_index_left;
            input_shared[shared_idx] = 
                (halo_index_left < 0) ? 0 : input[input_idx];
        }
    }
    
    shared_idx = threadIdx.y*shared_width+halo_width+threadIdx.x;
    input_shared[shared_idx] = (j < width && i < height) ? input[lin_idx] : 0;
       
    if (i < height) {
        int halo_index_right = (blockIdx.x+1)*blockDim.x+threadIdx.x;
        if (threadIdx.x < halo_width) {
            shared_idx = threadIdx.y*shared_width+blockDim.x+threadIdx.x+halo_width;
            input_idx = row_start+halo_index_right;
            input_shared[shared_idx] =
                (halo_index_right >= width) ? 0 : input[input_idx];
        }
    }
    __syncthreads();
  
    unsigned int filter_idx, target_idx;
    if (!(j%%stride) && i < height && j < width) {
        for (int f=0; f < n_filters; f++) {
            %(data_type)s Pvalue = 0.;
            for (int k=0; k < filter_width; k++) {
                shared_idx = threadIdx.y*shared_width+threadIdx.x+k;
                filter_idx = f*filter_width+k;
                Pvalue += input_shared[shared_idx]*filter[filter_idx];
            }
            target_idx = f*target_width*height+i*target_width+j/stride;
            target[target_idx] = Pvalue;
        }        
    }
}

__global__ void conv1d_grad_weights(const %(data_type)s *input,
    const %(data_type)s *df_output,
    %(data_type)s *df_weights,
    const unsigned int width, const unsigned int height,
    const unsigned int filter_width, const unsigned int n_filters) {
    
    const unsigned int i = blockIdx.y*blockDim.y+threadIdx.y;
    const unsigned int j = blockIdx.x*blockDim.x+threadIdx.x;
    const unsigned int tid = threadIdx.y*TILE_SIZE+threadIdx.x;
    const unsigned int lin_idx = i*width+j;
    const unsigned int row_start = i*width;
    unsigned int shared_idx, input_idx, df_output_idx;
    
    const unsigned int shared_width = TILE_SIZE+MAX_WIDTH_FILTER-1;
    __shared__ %(data_type)s input_shared[TILE_SIZE*shared_width];
    __shared__ %(data_type)s df_output_shared[TILE_SIZE*TILE_SIZE];
    __shared__ %(data_type)s df_weights_reduce[TILE_SIZE*TILE_SIZE];
    
    const unsigned int halo_width = filter_width / 2;

    // Load left halo elements
    if (i < height) {
        int halo_index_left = (blockIdx.x-1)*blockDim.x+threadIdx.x;
        if (threadIdx.x >= blockDim.x-halo_width) {
            shared_idx = threadIdx.y*shared_width + 
                threadIdx.x-(blockDim.x-halo_width);
            input_idx = row_start + halo_index_left;
            input_shared[shared_idx] = 
                (halo_index_left < 0) ? 0 : input[input_idx];
        }
    }
    
    // Load central elements
    shared_idx = threadIdx.y*shared_width+halo_width+threadIdx.x;
    input_shared[shared_idx] = (j < width && i < height) ? input[lin_idx] : 0;
    
    // Load right halo elements
    if (i < height) {
        int halo_index_right = (blockIdx.x+1)*blockDim.x+threadIdx.x;
        if (threadIdx.x < halo_width) {
            shared_idx = threadIdx.y*shared_width+blockDim.x+threadIdx.x+halo_width;
            input_idx = row_start+halo_index_right;
            input_shared[shared_idx] =
                (halo_index_right >= width) ? 0 : input[input_idx];
        }
    }
    __syncthreads();

    
    unsigned int target_idx;
    for (int f=0; f < n_filters; f++) {
        // Load df_output into shared memory
        df_output_idx = f*width*height+lin_idx;
        df_output_shared[tid] = (j < width && i < height) ?
            df_output[df_output_idx] : 0;

        // Compute df_weights for each vector element
        for (int k=0; k < filter_width; k++) {
            shared_idx = threadIdx.y*shared_width+threadIdx.x+k;
            df_weights_reduce[tid] = input_shared[shared_idx]*df_output_shared[tid];

            __syncthreads();

            // Reduction
            for (unsigned int s=TILE_SIZE*TILE_SIZE/2; s>0; s>>=1) {
                if (tid<s) {
                    df_weights_reduce[tid] += df_weights_reduce[tid+s];
                }
                __syncthreads();
            }

            if (tid==0) {
                target_idx = f*filter_width*gridDim.x*gridDim.y+
                    k*gridDim.x*gridDim.y+blockIdx.y*gridDim.x+blockIdx.x;
                df_weights[target_idx] = df_weights_reduce[0];
            }
            __syncthreads();
        }
    }
}

__global__ void conv1d_grad_weights_sum(const %(data_type)s *df_weights,
    %(data_type)s *df_weights_sum, const unsigned int n_filters,
    const unsigned int filter_width, const unsigned int n_elements) {

    const unsigned int tid = threadIdx.x;
    const unsigned int df_weights_idx = blockIdx.x*filter_width*n_elements+
        blockIdx.y*n_elements+threadIdx.x;
    
    extern __shared__ %(data_type)s sdata[];
    
    sdata[tid] = (tid<n_elements) ? df_weights[df_weights_idx] : 0;
    __syncthreads();
    
    for (unsigned int s=blockDim.x/2; s>0; s>>=1) {
        if (tid < s) {
            sdata[tid] += sdata[tid+s];
        }
        __syncthreads();
    }
    
    if (tid==0) {        
        const unsigned int df_weights_sum_idx = blockIdx.x*filter_width+blockIdx.y;
        df_weights_sum[df_weights_sum_idx] = sdata[0];
    }
}
"""

source_module_float = SourceModule(code % {'TILE_SIZE': CONV_BLOCK_SIZE[0],
                                           'MAX_WIDTH_FILTER': MAX_WIDTH_FILTER,
                                           'MAX_NUM_FILTERS': MAX_NUM_FILTERS,
                                           'data_type': 'float'})
source_module_double = SourceModule(code % {'TILE_SIZE': CONV_BLOCK_SIZE[0],
                                            'MAX_WIDTH_FILTER': MAX_WIDTH_FILTER,
                                            'MAX_NUM_FILTERS': MAX_NUM_FILTERS,
                                            'data_type': 'double'})

conv1d_matrix_mult_filter_kernel_float = source_module_float.get_function('conv1d_matrix_mult_filter')
conv1d_grad_weights_kernel_float = source_module_float.get_function('conv1d_grad_weights')
conv1d_grad_weights_sum_kernel_float = source_module_float.get_function('conv1d_grad_weights_sum')
conv1d_matrix_mult_filter_kernel_double = source_module_double.get_function('conv1d_matrix_mult_filter')
conv1d_grad_weights_kernel_double = source_module_double.get_function('conv1d_grad_weights')
conv1d_grad_weights_sum_kernel_double = source_module_double.get_function('conv1d_grad_weights_sum')

def conv1d_matrix_mult_filter(mat, conv_filter, stride=1, target=None, stream=None):
    dtype = mat.dtype
    assert dtype in (np.float32, np.float64)
    assert conv_filter.dtype == dtype
    
    if target is not None:
        assert target.dtype == dtype
        assert mat.shape // stride == target.shape[:2]

    height, width = mat.shape
    n_filters, width_filter = conv_filter.shape
    
    block = CONV_BLOCK_SIZE
    grid = (int(np.ceil(mat.shape[1] / float(block[0]))),
            int(np.ceil(mat.shape[0] / float(block[1]))),
            1)
    
    if target is None:
        target_width = int(np.ceil(width / float(stride)))
        target = gpuarray.empty((n_filters, height, target_width),
                                dtype=dtype)

    if dtype == np.float32:
        kernel = conv1d_matrix_mult_filter_kernel_float
    elif dtype == np.float64:
        kernel = conv1d_matrix_mult_filter_kernel_double
    else:
        raise ValueError

    kernel(mat, target, conv_filter,
           np.int32(width), np.int32(height),
           np.int32(width_filter),
           np.int32(n_filters),
           np.int32(stride),
           block=block, grid=grid,
           stream=stream)
        
    return target

def conv1d_grad_weights(mat, df_output, filter_width, n_filters, 
                        target=None, stream=None):
    dtype = mat.dtype
    assert dtype in (np.float32, np.float64)
    assert df_output.dtype == dtype

    height, width = mat.shape

    block = CONV_BLOCK_SIZE
    grid = (int(np.ceil(mat.shape[1] / float(block[0]))),
            int(np.ceil(mat.shape[0] / float(block[1]))),
            1)

    if target is not None:
        assert target.dtype == dtype
        assert target.shape == (n_filters, filter_width)
    else:
        target = gpuarray.empty((n_filters, filter_width, 
                                 grid[1], grid[0]), dtype=dtype)

    if dtype == np.float32:
        kernel = conv1d_grad_weights_kernel_float
        kernel_sum = conv1d_grad_weights_sum_kernel_float
    elif dtype == np.float64:
        kernel = conv1d_grad_weights_kernel_double
        kernel_sum = conv1d_grad_weights_sum_kernel_double
    else:
        raise ValueError

    kernel(mat, df_output, target,
           np.int32(width), np.int32(height),
           np.int32(filter_width), np.int32(n_filters),
           block=block, grid=grid, stream=stream)

    sum_height = grid[1]
    sum_width = grid[0]
    target_sum = gpuarray.empty((n_filters, filter_width), dtype)
    block_sum = (2**int(np.ceil(np.log2(sum_height*sum_width))), 1, 1)
    grid_sum = (n_filters, filter_width, 1)
    shared = block_sum[0]*np.dtype(dtype).itemsize
    kernel_sum(target, target_sum,
               np.int32(n_filters), np.int32(filter_width),
               np.int32(sum_height*sum_width),
               block=block_sum, grid=grid_sum, shared=shared)

    return target_sum
