import time

from .peeler_tools import *
from .peeler_tools import _dtype_spike
from .tools import make_color_dict

from .signalpreprocessor import signalpreprocessor_engines
from .peakdetector import peakdetector_engines


from .cltools import HAVE_PYOPENCL, OpenCL_Helper
if HAVE_PYOPENCL:
    import pyopencl
    mf = pyopencl.mem_flags

from . import pythran_tools
if hasattr(pythran_tools, '__pythran__'):
    HAVE_PYTHRAN = True
else:
    HAVE_PYTHRAN = False

try:
    import numba
    HAVE_NUMBA = True
    from .numba_tools import numba_loop_sparse_dist
except ImportError:
    HAVE_NUMBA = False


import matplotlib.pyplot as plt


class PeelerEngineBase(OpenCL_Helper):
    def change_params(self, catalogue=None, chunksize=1024, 
                                        internal_dtype='float32', 
                                        use_sparse_template=False,
                                        sparse_threshold_mad=1.5,
                                        argmin_method='numpy',
                                        
                                        maximum_jitter_shift = 4,
                                        
                                        cl_platform_index=None,
                                        cl_device_index=None,
                                        
                                        
                                        
                                        ):
        """
        Set parameters for the Peeler.
        
        
        Parameters
        ----------
        catalogue: the catalogue (a dict)
            The catalogue made by CatalogueConstructor.
        chunksize: int (1024 by default)
            the size of chunk for processing.
        internal_dtype: 'float32' or 'float64'
            dtype of internal processing. float32 is OK. float64 is totally useless.
        use_sparse_template: bool (dafult False)
            For very high channel count, centroids from catalogue can be sparcifyed.
            The speedup a lot the process but the sparse_threshold_mad must be
            set carrefully and compared with use_sparse_template=False.
            For low channel count this is useless.
        sparse_threshold_mad: float (1.5 by default)
            The threshold level.
            Under this value if all sample on one channel for one centroid
            is considred as NaN
        argmin_method: 'numpy', 'opencl', 'pythran' or 'numba'
            Method use to compute teh minial distance to template.
        """
        assert catalogue is not None
        self.catalogue = catalogue
        self.chunksize = chunksize
        self.internal_dtype= internal_dtype
        self.use_sparse_template = use_sparse_template
        self.sparse_threshold_mad = sparse_threshold_mad
        
        self.argmin_method = argmin_method
        
        self.maximum_jitter_shift = maximum_jitter_shift

        self.cl_platform_index=None
        self.cl_device_index=None
        
        
        
        # Some check
        if self.use_sparse_template:
            assert self.argmin_method != 'numpy', 'numpy methdo do not do sparse template acceleration'

            if self.argmin_method == 'opencl':
                assert HAVE_PYOPENCL, 'OpenCL is not available'
            elif self.argmin_method == 'pythran':
                assert HAVE_PYTHRAN, 'Pythran is not available'
            elif self.argmin_method == 'numba':
                assert HAVE_NUMBA, 'Numba is not available'
            
        self.colors = make_color_dict(self.catalogue['clusters'])
        
        # precompute some value for jitter estimation
        n = self.catalogue['cluster_labels'].size
        self.catalogue['wf1_norm2'] = np.zeros(n)
        self.catalogue['wf2_norm2'] = np.zeros(n)
        self.catalogue['wf1_dot_wf2'] = np.zeros(n)
        for i, k in enumerate(self.catalogue['cluster_labels']):
            chan = self.catalogue['max_on_channel'][i]
            wf0 = self.catalogue['centers0'][i,: , chan]
            wf1 = self.catalogue['centers1'][i,: , chan]
            wf2 = self.catalogue['centers2'][i,: , chan]

            self.catalogue['wf1_norm2'][i] = wf1.dot(wf1)
            self.catalogue['wf2_norm2'][i] = wf2.dot(wf2)
            self.catalogue['wf1_dot_wf2'][i] = wf1.dot(wf2)
        
        
        #~ print('self.use_sparse_template', self.use_sparse_template)
        
        centers = self.catalogue['centers0']
        #~ print(centers.shape)
        if self.use_sparse_template:
            #~ print(centers.shape)
            # TODO use less memory
            self.sparse_mask = np.any(np.abs(centers)>sparse_threshold_mad, axis=1)
        else:
            self.sparse_mask = np.ones((centers.shape[0], centers.shape[2]), dtype='bool')
        
        #~ print('self.sparse_mask.shape', self.sparse_mask.shape)
        self.weight_per_template = {}
        for i, k in enumerate(self.catalogue['cluster_labels']):
            mask = self.sparse_mask[i, :]
            wf = centers[i, :, :][:, mask]
            self.weight_per_template[k] = np.sum(wf**2, axis=0)
            #~ print(wf.shape, self.weight_per_template[k].shape)

        #~ for i in range(centers.shape[0]):
            #~ fig, ax = plt.subplots()
            #~ center = centers[i,:,:].copy()
            #~ center_sparse = center.copy()
            #~ center_sparse[:, ~mask[i, :]] = 0.
            #~ ax.plot(center.T.flatten(), color='g')
            #~ ax.plot(center_sparse.T.flatten(), color='r', ls='--')
            #~ ax.axhline(sparse_threshold_mad)
            #~ ax.axhline(-sparse_threshold_mad)
            #~ plt.show()


    def initialize_before_each_segment(self, sample_rate=None, nb_channel=None, source_dtype=None, geometry=None):
        
        self.nb_channel = nb_channel
        self.sample_rate = sample_rate
        self.source_dtype = source_dtype
        self.geometry = geometry
        
        # signal processor class
        self.signalpreprocessor_engine = self.catalogue['signal_preprocessor_params']['signalpreprocessor_engine']
        SignalPreprocessor_class = signalpreprocessor_engines[self.signalpreprocessor_engine]
        self.signalpreprocessor = SignalPreprocessor_class(sample_rate, nb_channel, self.chunksize, source_dtype)
        p = dict(self.catalogue['signal_preprocessor_params'])
        p.pop('signalpreprocessor_engine')
        p['normalize'] = True
        p['signals_medians'] = self.catalogue['signals_medians']
        p['signals_mads'] = self.catalogue['signals_mads']
        self.signalpreprocessor.change_params(**p)
        
        self.internal_dtype = self.signalpreprocessor.output_dtype
        
        assert self.chunksize>self.signalpreprocessor.lostfront_chunksize, 'lostfront_chunksize ({}) is greater than chunksize ({})!'.format(self.signalpreprocessor.lostfront_chunksize, self.chunksize)

        # peak detecetor class
        p = dict(self.catalogue['peak_detector_params'])
        peakdetector_engine = p.pop('peakdetector_engine', 'numpy') # TODO put engine in info json back
        PeakDetector_class = peakdetector_engines[peakdetector_engine]
        self.peakdetector = PeakDetector_class(self.sample_rate, self.nb_channel,
                                                        self.chunksize, self.internal_dtype, self.geometry)
        self.peakdetector.change_params(**p)

        self.peak_sign = self.catalogue['peak_detector_params']['peak_sign']
        self.relative_threshold = self.catalogue['peak_detector_params']['relative_threshold']
        peak_span_ms = self.catalogue['peak_detector_params']['peak_span_ms']
        self.n_span = int(sample_rate * peak_span_ms / 1000.)//2
        self.n_span = max(1, self.n_span)
        self.peak_width = self.catalogue['peak_width']
        self.n_side = self.catalogue['peak_width'] + self.maximum_jitter_shift + self.n_span + 1
        self.n_right = self.catalogue['n_right']
        self.n_left = self.catalogue['n_left']
        
        assert self.chunksize > (self.n_side+1), 'chunksize is too small because of n_size'
        
        self.alien_value_threshold = self.catalogue['clean_waveforms_params']['alien_value_threshold']
        
        self.total_spike = 0
        
        self.near_border_good_spikes = []
        
        self.fifo_residuals = np.zeros((self.n_side+self.chunksize, nb_channel), 
                                                                dtype=self.internal_dtype)
        
        

    def get_remaining_spikes(self):
        if len(self.near_border_good_spikes)>0:
            # deal with extra remaining spikes
            extra_spikes = self.near_border_good_spikes[0]
            extra_spikes = extra_spikes.take(np.argsort(extra_spikes['index']))
            self.total_spike += extra_spikes.size
            return extra_spikes




class PeelerEngineGeneric(PeelerEngineBase):

    def process_one_chunk(self,  pos, sigs_chunk):
        #~ print('process_one_chunk', pos)
        
        #~ print('*'*10)
        t1 = time.perf_counter()
        abs_head_index, preprocessed_chunk = self.signalpreprocessor.process_data(pos, sigs_chunk)
        #~ t2 = time.perf_counter()
        #~ print('process_data', (t2-t1)*1000)
        
        
        #shift rsiruals buffer and put the new one on right side
        t1 = time.perf_counter()
        fifo_roll_size = self.fifo_residuals.shape[0]-preprocessed_chunk.shape[0]
        if fifo_roll_size>0 and fifo_roll_size!=self.fifo_residuals.shape[0]:
            self.fifo_residuals[:fifo_roll_size,:] = self.fifo_residuals[-fifo_roll_size:,:]
            self.fifo_residuals[fifo_roll_size:,:] = preprocessed_chunk
        #~ t2 = time.perf_counter()
        #~ print('fifo move', (t2-t1)*1000.)

        
        # relation between inside chunk index and abs index
        to_local_shift = abs_head_index - self.fifo_residuals.shape[0]
        
        
        
        self.detect_local_peaks_before_peeling_loop()
        #~ self._debug_nb_accept_tempate = 0
        
        good_spikes = []
        
        n_loop = 0
        t3 = time.perf_counter()
        while True:
            #~ print('peeler level +1')
            nb_good_spike = 0
            peak_ind, peak_chan = self.select_next_peak()
            
            #~ print('start inner loop')
            while peak_ind != LABEL_NO_MORE_PEAK:
            
                #~ print('  peak_ind', peak_ind)
                #~ t2 = time.perf_counter()
                #~ print('  select_next_peak', (t2-t1)*1000)
                
                #~ if peak_ind == LABEL_NO_MORE_PEAK:
                    #~ print('break inner loop 1')
                    #~ break
                
                
                if 16000 <(peak_ind+to_local_shift)<16400:
                    self._plot_debug = True
                else:
                    self._plot_debug = True
                
                t1 = time.perf_counter()
                spike = self.classify_and_align_next_spike(peak_ind, peak_chan)
                #~ t2 = time.perf_counter()
                #~ print('  classify_and_align_next_spike', (t2-t1)*1000)
                #~ if spike.cluster_label <0:
                    #~ print('   spike.label', spike.cluster_label, 'peak_ind, peak_chan', peak_ind, peak_chan)

                #~ print('spike', spike.index+to_local_shift)
                
                
                
                if spike.cluster_label == LABEL_NO_MORE_PEAK:
                    #~ print('break inner loop 1')
                    break
                
                if (spike.cluster_label >=0):
                    #~ good_spikes.append(np.array([spike], dtype=_dtype_spike))
                    good_spikes.append(spike)
                    nb_good_spike+=1
                    
                    # remove from residulals
                    self.on_accepted_spike(spike)
                else:
                    
                    self.set_already_tested(peak_ind, peak_chan)
                    

                peak_ind, peak_chan = self.select_next_peak()
                
                #~ # debug
                n_loop +=1 
                
                
                #~ import matplotlib.pyplot as plt
                #~ from .peakdetector import make_sum_rectified
                #~ #print('spike', spike)
                #~ fig, ax = plt.subplots()
                #~ ax.plot(self.fifo_residuals)
                #~ ax.plot(np.arange(self.mask_not_already_tested.size) + self.n_span, self.mask_not_already_tested.astype(float)*10, color='k')
                #~ local_peaks,  = np.nonzero(self.local_peaks_mask & self.mask_not_already_tested)
                #~ local_peaks += self.n_span
                #~ sum_rectified = make_sum_rectified(self.fifo_residuals, self.peakdetector.relative_threshold, self.peakdetector.peak_sign, self.peakdetector.spatial_matrix)
                #~ ax.scatter(local_peaks, np.min(self.fifo_residuals[local_peaks, :], axis=1), color='k')
                #~ #ax.plot(sum_rectified, color='k', lw=1.5)
                #~ #ax.scatter(local_peaks, sum_rectified[local_peaks], color='k')
                #~ for p in local_peaks:
                    #~ ax.axvline(p, color='k', ls='--')
                #~ ax.axvline(peak_ind, color='r', ls='-')
                #~ ax.set_ylim(-300, 100)
                #~ ax.set_ylim(-12, 4)
                #~ plt.show()
            
            if nb_good_spike == 0:
                #~ print('break main loop')

                
                #~ fig, ax = plt.subplots()
                #~ plot_sigs = self.fifo_residuals.copy()
                #~ for c in range(self.nb_channel):
                    #~ plot_sigs[:, c] += c*30
                #~ ax.plot(plot_sigs, color='k')
                #~ ax.axvline(self.fifo_residuals.shape[0] - self.n_right)
                #~ ax.scatter([left_ind-self.n_left], [self.fifo_residuals[left_ind-self.n_left, max_chan_ind]], color='r')
                
                #~ bad_spikes = self.get_no_label_peaks()
                #~ for s in self.get_no_label_peaks():
                    #~ ax.axvline(s['index'], color='r')
                #~ mask = self.peakdetector.get_mask_peaks_in_chunk(self.fifo_residuals)
                #~ nolabel_indexes, chan_indexes = np.nonzero(mask)
                #~ nolabel_indexes = nolabel_indexes + self.n_span
                #~ ax.scatter(nolabel_indexes, plot_sigs[nolabel_indexes, chan_indexes], color='r')
                    

                
                
                plt.show()                
                
                break
            else:
                
                self.reset_to_not_tested(good_spikes[-nb_good_spike:])
                
                #~ t2 = time.perf_counter()
                #~ print('  update mask', (t2-t1)*1000)
        
        
        if self._plot_debug:
            self._plot_empty_fifo()
        
        #~ print(self._debug_nb_accept_tempate)
        #~ t4 = time.perf_counter()
        #~ print('mainloop classify_and_align some spike', (t4-t3)*1000)
        #~ print('nb_good_spike', len(good_spikes), 'n_loop', n_loop, 'per spike', (t4-t3)*1000/len(good_spikes))
        
        bad_spikes = self.get_no_label_peaks()
        bad_spikes['index'] += to_local_shift
        

        
        if len(good_spikes)>0:
            # TODO remove from peak the very begining of the signal because of border filtering effects
            
            good_spikes = np.array(good_spikes, dtype=_dtype_spike)
            good_spikes['index'] += to_local_shift
            near_border = (good_spikes['index'] - to_local_shift)>=(self.chunksize+self.n_span)
            near_border_good_spikes = good_spikes[near_border].copy()
            good_spikes = good_spikes[~near_border]

            all_spikes = np.concatenate([good_spikes] + [bad_spikes] + self.near_border_good_spikes)
            self.near_border_good_spikes = [near_border_good_spikes] # for next chunk
        else:
            all_spikes = np.concatenate([bad_spikes] + self.near_border_good_spikes)
            self.near_border_good_spikes = []
        
        # all_spikes = all_spikes[np.argsort(all_spikes['index'])]
        all_spikes = all_spikes.take(np.argsort(all_spikes['index']))
        self.total_spike += all_spikes.size
        
        #~ print(good_spikes.size, all_spikes.size)
        #~ exit()
        return abs_head_index, preprocessed_chunk, self.total_spike, all_spikes

    def classify_and_align_next_spike(self, proposed_peak_ind, peak_chan):
        #~ if self._plot_debug:
            #~ print('classify_and_align_next_spike')
        # left_ind is the waveform left border
        left_ind = proposed_peak_ind + self.n_left

        #~ if left_ind+self.peak_width+self.maximum_jitter_shift+1>=self.fifo_residuals.shape[0]:
        if left_ind+self.peak_width + 1>=self.fifo_residuals.shape[0]:
            # TODO : remove this because maybe unecessry
            # too near right limits no label
            label = LABEL_RIGHT_LIMIT
            jitter = 0
            #~ if self._plot_debug:
                #~ print('LABEL_RIGHT_LIMIT', proposed_peak_ind, peak_chan)
        #~ elif left_ind<=self.maximum_jitter_shift:
        elif left_ind<0:
            # TODO : remove this because maybe unecessry
            # too near left limits no label
            #~ print('     LABEL_LEFT_LIMIT', left_ind)
            label = LABEL_LEFT_LIMIT
            jitter = 0
            #~ if self._plot_debug:
                #~ print('LABEL_LEFT_LIMIT', proposed_peak_ind, peak_chan)
        elif self.catalogue['centers0'].shape[0]==0:
            # empty catalogue
            label  = LABEL_UNCLASSIFIED
            jitter = 0
            #~ if self._plot_debug:
                #~ print('LABEL_UNCLASSIFIED', proposed_peak_ind, peak_chan)
        else:
            waveform = self.fifo_residuals[left_ind:left_ind+self.peak_width,:]
            
            if self.alien_value_threshold is not None and \
                    np.any((waveform>self.alien_value_threshold) | (waveform<-self.alien_value_threshold)) :
                label  = LABEL_ALIEN
                jitter = 0
                #~ if self._plot_debug:
                    #~ print('LABEL_ALIEN', proposed_peak_ind, peak_chan)

            else:
                
                t1 = time.perf_counter()
                #TODO try usewaveform to avoid new buffer ????
                
                cluster_idx = self.get_best_template(left_ind, peak_chan)
                #~ t2 = time.perf_counter()
                #~ print('    get_best_template', (t2-t1)*1000)
                


                
                
                #~ t1 = time.perf_counter()
                #~ print('left_ind', left_ind, 'proposed_peak_ind', proposed_peak_ind)
                jitter = self.estimate_jitter(left_ind, cluster_idx)
                #~ t2 = time.perf_counter()
                #~ print('    estimate_jitter', (t2-t1)*1000)
                
                t1 = time.perf_counter()
                ok = self.accept_tempate(left_ind, cluster_idx, jitter)
                #~ t2 = time.perf_counter()
                #~ print('    accept_tempate', (t2-t1)*1000)

                # DEBUG
                #~ label = self.catalogue['cluster_labels'][cluster_idx]
                #~ if label in (5, 8):
                #~ if label in (10, ):
                    #~ print('label', label, 'ok', ok, 'jitter', jitter)
                # END DEBUG                

                if  not ok:
                    label  = LABEL_UNCLASSIFIED
                    jitter = 0
                else:
                    #~ print('cluster_idx', cluster_idx, 'jitter', jitter)
                    shift = -int(np.round(jitter))
                    if (np.abs(jitter) > 0.5) and \
                            (left_ind+shift+self.peak_width<self.fifo_residuals.shape[0]) and\
                            ((left_ind + shift) >= 0):
                        #~ shift = -int(np.round(jitter))
                        
                        # debug
                        #~ new_cluster_idx = self.get_best_template(left_ind+shift)
                        #~ new_jitter = self.estimate_jitter(left_ind + shift, new_cluster_idx)
                        #~ ok = self.accept_tempate(left_ind+shift, new_cluster_idx, new_jitter)
                        # end debug
                        new_jitter = self.estimate_jitter(left_ind + shift, cluster_idx)
                        ok = self.accept_tempate(left_ind+shift, cluster_idx, new_jitter)
                        if ok and np.abs(new_jitter)<np.abs(jitter):
                            jitter = new_jitter
                            left_ind += shift
                            shift = -int(np.round(jitter))
                            
                            # debug
                            #~ if cluster_idx != new_cluster_idx:
                                #~ print('cluster_idx != new_cluster_idx')
                            #~ cluster_idx = new_cluster_idx
                            
                            
                    
                    # ensure jitter in range [-0.5, 0.5]
                    # WRONG IDEA because the mask_not_already_tested will not updated at the good place
                    #~ if shift !=0:
                        #~ jitter = jitter + shift
                        #~ left_ind = left_ind + shift
                    
                    # security to not be outside the fifo
                    if np.abs(shift) >self.maximum_jitter_shift:
                        label = LABEL_MAXIMUM_SHIFT
                        
                    elif (left_ind+shift+self.peak_width)>=self.fifo_residuals.shape[0]:
                        # normally this should be resolve in the next chunk
                        label = LABEL_RIGHT_LIMIT
                    elif (left_ind + shift) < 0:
                        # TODO assign the previous label ???
                        label = LABEL_LEFT_LIMIT
                    else:
                        label = self.catalogue['cluster_labels'][cluster_idx]

        #security if with jitter the index is out
        if label>=0:
            left_ind_check = left_ind - np.round(jitter).astype('int64')
            if left_ind_check<0:
                label = LABEL_LEFT_LIMIT
                if self._plot_debug:
                    print('!!!!!!!ici LABEL_LEFT_LIMIT', label)

            elif (left_ind_check+self.peak_width) >=self.fifo_residuals.shape[0]:
                label = LABEL_RIGHT_LIMIT
                if self._plot_debug:
                    print('!!!!!!!ici LABEL_RIGHT_LIMIT', label)
                
        
        #~ if self._plot_debug:
            #~ if label in (LABEL_LEFT_LIMIT, LABEL_RIGHT_LIMIT, LABEL_UNCLASSIFIED):
                #~ fig, ax = plt.subplots()
                #~ waveform = self.fifo_residuals[left_ind:left_ind+self.peak_width,:]
                #~ ax.plot(waveform.T.flatten())
                
                #~ if label == LABEL_LEFT_LIMIT:
                    #~ ax.set_title('LABEL_LEFT_LIMIT')
                #~ if label == LABEL_RIGHT_LIMIT:
                    #~ ax.set_title('LABEL_RIGHT_LIMIT')
                #~ if label == LABEL_UNCLASSIFIED:
                    #~ ax.set_title('LABEL_UNCLASSIFIED')
                    


                #~ max_chan_ind = np.argmax(np.abs(waveform[-self.n_left, :]))
                #~ fig, ax = plt.subplots()
                #~ ax.plot(self.fifo_residuals[:, max_chan_ind])
                
                #~ ax.scatter([left_ind-self.n_left], [self.fifo_residuals[left_ind-self.n_left, max_chan_ind]], color='r')
                
                
                #~ plt.show()
        
        
        if label < 0:
            # set peak tested to not test it again
            #~ self.mask_not_already_tested[proposed_peak_ind - self.n_span] = False
            peak_ind = proposed_peak_ind

        #~ self.update_peak_mask(peak_ind, label)
        #~ t2 = time.perf_counter()
        #~ print('    update_peak_mask', (t2-t1)*1000)
        else:
            # ensure jitter in range [-0.5, 0.5]
            shift = -int(np.round(jitter))
            if shift !=0:
                jitter = jitter + shift
                left_ind = left_ind + shift
            
            peak_ind = left_ind - self.n_left
        
        return Spike(peak_ind, label, jitter)

    def estimate_jitter(self, left_ind, cluster_idx):
        
        chan_max = self.catalogue['max_on_channel'][cluster_idx]
        
        wf0 = self.catalogue['centers0'][cluster_idx,: , chan_max]
        wf1 = self.catalogue['centers1'][cluster_idx,: , chan_max]
        wf2 = self.catalogue['centers2'][cluster_idx,: , chan_max]

        wf = self.fifo_residuals[left_ind:left_ind+self.peak_width,chan_max]
        
        
        #it is  precompute that at init for speedup
        wf1_norm2= self.catalogue['wf1_norm2'][cluster_idx]
        wf2_norm2 = self.catalogue['wf2_norm2'][cluster_idx]
        wf1_dot_wf2 = self.catalogue['wf1_dot_wf2'][cluster_idx]
        
        h = wf - wf0
        h0_norm2 = h.dot(h)
        h_dot_wf1 = h.dot(wf1)
        jitter0 = h_dot_wf1/wf1_norm2
        h1_norm2 = np.sum((h-jitter0*wf1)**2)
        #~ print(h0_norm2, h1_norm2)
        #~ print(h0_norm2 > h1_norm2)
        
        if h0_norm2 > h1_norm2:
            #order 1 is better than order 0
            h_dot_wf2 = np.dot(h,wf2)
            rss_first = -2*h_dot_wf1 + 2*jitter0*(wf1_norm2 - h_dot_wf2) + 3*jitter0**2*wf1_dot_wf2 + jitter0**3*wf2_norm2
            rss_second = 2*(wf1_norm2 - h_dot_wf2) + 6*jitter0*wf1_dot_wf2 + 3*jitter0**2*wf2_norm2
            jitter1 = jitter0 - rss_first/rss_second
            #~ h2_norm2 = np.sum((h-jitter1*wf1-jitter1**2/2*wf2)**2)
            #~ if h1_norm2 <= h2_norm2:
                #when order 2 is worse than order 1
                #~ jitter1 = jitter0
        else:
            jitter1 = 0.
        
        return jitter1

