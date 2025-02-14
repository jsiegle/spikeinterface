import numpy as np
from tqdm.auto import tqdm, trange
import scipy.interpolate


possible_motion_estimation_methods = ['decentralized_registration', 'iterative_template_registration']


def init_kwargs_dict(method, method_kwargs):
    # handle kwargs by method
    if method == 'decentralized_registration':
        method_kwargs_ = dict(pairwise_displacement_method='conv',
                              convergence_method='gradient_descent',
                              max_displacement_um=1500)
    elif method == 'iterative_template_registration':
        method_kwargs_ = dict(num_amp_bins=20,
                              num_shifts_global=15,
                              num_iterations=10,
                              num_shifts_block=5,
                              non_rigid_window_overlap=0.5,
                              smoothing_sigma=0.5,
                              kriging_sigma=1,
                              kriging_p=2,
                              kriging_d=2)
    method_kwargs_.update(method_kwargs)
    return method_kwargs_


def estimate_motion(recording, peaks, peak_locations,
                    direction='y', bin_duration_s=10., bin_um=10., margin_um=50,
                    method='decentralized_registration', method_kwargs={},
                    non_rigid_kwargs=None, clean_motion_kwargs=None, 
                    output_extra_check=False, progress_bar=False,
                    upsample_to_histogram_bin=None, verbose=False):
    """
    Estimate motion given peaks and their localization.

    Parameters
    ----------
    recording: BaseRecording
        The recording extractor
    peaks: numpy array
        Peak vector (complex dtype)
    peak_locations: numpy array
        Complex dtype with 'x', 'y', 'z' fields
    direction: 'x', 'y', 'z'
        Dimension on which the motion is estimated
    bin_duration_s: float
        Bin duration in second
    bin_um: float
        Spatial bin size in micro meter
    margin_um: float
        Margin in um to exclude from histogram estimation and
        non-rigid smoothing functions to avoid edge effects
    method: str
        The method to be used ('decentralized_registration', 'iterative_template_registration')
    method_kwargs: dict
        Specific options for the chosen method. You can use `init_kwargs_dict(method)` to retrieve default params.
        * 'decentralized_registration' (see https://proceedings.neurips.cc/paper/2021/hash/b950ea26ca12daae142bd74dba4427c8-Abstract.html)
        * 'iterative_template_registration' (see https://www.science.org/doi/abs/10.1126/science.abf4588 - Kilosort2.5 method)
    non_rigid_kwargs: None or dict.
        If None then the motion is consider as rigid.
        If dict then the motion is estimated in non rigid manner with fields:
        * bin_step_um: step in um to construct overlapping gaussian smoothing functions
    clean_motion_kwargs: None or dict
        If None then the clean_motion_vector() is apply on the vector to
        remove the spurious fast bump in the motion vector.
        Can also apply optional a smoothing.
    output_extra_check: bool
        If True then return an extra dict that contains variables
        to check intermediate steps (motion_histogram, non_rigid_windows, pairwise_displacement)
    upsample_to_histogram_bin: bool or None
        If True then upsample the returned motion array to the number of depth bins specified
        by bin_um.
        When None:
          * for non rigid case: then automatically True
          * for rigid (non_rigid_kwargs=None): automatically False
    progress_bar: bool
        Display progress bar or not.
    verbose: bool
        If True, output is verbose

    Returns
    -------
    motion: numpy array 2d
        Motion estimate in um.
        Shape (temporal bins, spatial bins)
        motion.shape[0] = temporal_bins.shape[0]
        motion.shape[1] = 1 (rigid) or spatial_bins.shape[1] (non rigid)
        If upsample_to_histogram_bin, motion.shape[1] corresponds to spatial
        bins given by bin_um.
    temporal_bins: numpy.array 1d
        temporal bins (bin center)
    spatial_bins: numpy.array 1d or None
        If rigid then None
        else motion.shape[1]
    extra_check: dict
        Optional output if `output_extra_check=True`
        This dict contain histogram, pairwise_displacement usefull for ploting.
    """
    # TODO handle multi segment one day
    assert recording.get_num_segments() == 1
    assert method in possible_motion_estimation_methods
    method_kwargs = init_kwargs_dict(method, method_kwargs)

    if output_extra_check:
        extra_check = {}

    # contact positions
    probe = recording.get_probe()
    dim = ['x', 'y', 'z'].index(direction)
    contact_pos = probe.contact_positions[:, dim]

    # spatial bins
    spatial_bin_edges = get_spatial_bin_edges(recording, direction, margin_um, bin_um)
    num_spatial_bins = len(spatial_bin_edges)

    # handle non-rigid for all estimation algorithms
    if non_rigid_kwargs is None:
        # unique block for all depths
        num_non_rigid_windows = 1
        non_rigid_windows = [np.ones(num_spatial_bins, dtype='float64')]
        spatial_bins_non_rigid = [spatial_bin_edges[num_spatial_bins // 2] + bin_um / 2]
    else:
        assert 'bin_step_um' in non_rigid_kwargs, "'non_rigid_kwargs' needs to specify the 'bin_step_um' field"
        bin_step_um = non_rigid_kwargs['bin_step_um']

        min_ = np.min(contact_pos) - margin_um
        max_ = np.max(contact_pos) + margin_um
        num_non_rigid_windows = int((max_ - min_) // bin_step_um)
        border = ((max_ - min_)  %  bin_step_um) / 2
        spatial_bins_non_rigid = np.arange(num_non_rigid_windows) * bin_step_um + min_ + border
        # non rigid windows need to be pre-computed for upsample_to_histogram_bin option
        sigma_um = non_rigid_kwargs.get('sigma', 3) * bin_step_um
        non_rigid_windows = []
        for win_center in spatial_bins_non_rigid:
            win = np.exp(-(spatial_bin_edges[:-1] - win_center) ** 2 / (sigma_um ** 2))
            non_rigid_windows.append(win)
        if output_extra_check:
            extra_check['non_rigid_windows'] = non_rigid_windows

    if method == 'decentralized_registration':
        # make 2D histogram raster
        if verbose:
            print('Computing motion histogram')
        motion_histogram, temporal_hist_bin_edges, spatial_hist_bin_edges = \
            make_2d_motion_histogram(recording, peaks,
                                     peak_locations,
                                     direction=direction,
                                     bin_duration_s=bin_duration_s,
                                     spatial_bin_edges=spatial_bin_edges)
        if output_extra_check:
            extra_check['motion_histogram'] = motion_histogram
            extra_check['pairwise_displacement_list'] = []

        # temporal bins are bin center
        temporal_bins = temporal_hist_bin_edges[:-1] + bin_duration_s // 2.

        motion = []
        windows_iter = non_rigid_windows
        if progress_bar:
            windows_iter = tqdm(windows_iter, desc="windows")
        for i, win in enumerate(windows_iter):
            window_slice = np.flatnonzero(win > 1e-5)
            window_slice = slice(window_slice[0], window_slice[-1])
            motion_hist = win[np.newaxis, window_slice] * motion_histogram[:, window_slice]
            if verbose:
                print(f'Computing pairwise displacement: {i + 1} / {len(non_rigid_windows)}')

            pairwise_displacement, pairwise_displacement_weight = compute_pairwise_displacement(
                motion_hist, bin_um,
                method=method_kwargs['pairwise_displacement_method'],
                weight_scale=method_kwargs.get("weight_scale", 'linear'),
                error_sigma=method_kwargs.get("error_sigma", 0.2),
                conv_engine=method_kwargs.get("conv_engine", 'numpy'),
                torch_device=method_kwargs.get("torch_device", None),
                batch_size=method_kwargs.get("batch_size", 1),
                max_displacement_um=method_kwargs.get("max_displacement_um", 1500), # TODO: unify arg with KS
                corr_threshold=method_kwargs.get("corr_threshold", 0),
                time_horizon_s=method_kwargs.get("time_horizon_s", None),
                bin_duration_s=bin_duration_s,
                progress_bar=False
            )
            if output_extra_check:
                extra_check['pairwise_displacement_list'].append(pairwise_displacement)

            if verbose:
                print(f'Computing global displacement: {i + 1} / {len(non_rigid_windows)}')

            one_motion = compute_global_displacement(
                pairwise_displacement,
                pairwise_displacement_weight=pairwise_displacement_weight,
                convergence_method=method_kwargs['convergence_method'],
                robust_regression_sigma=method_kwargs.get("robust_regression_sigma", 2),
                gradient_descent_max_iter=method_kwargs.get("gradient_descent_max_iter", 1000),
                lsqr_robust_n_iter=method_kwargs.get("lsqr_robust_n_iter", 20),
                progress_bar=False,
            )
            motion.append(one_motion[:, np.newaxis])
        motion = np.concatenate(motion, axis=1)

    elif method == "iterative_template_registration":
        motion_histograms, temporal_hist_bin_edges, spatial_hist_bin_edges = \
            make_3d_motion_histograms(recording, peaks,
                                      peak_locations,
                                      direction=direction,
                                      num_amp_bins=method_kwargs['num_amp_bins'],
                                      bin_duration_s=bin_duration_s,
                                      spatial_bin_edges=spatial_bin_edges)
        # temporal bins are bin center
        temporal_bins = temporal_hist_bin_edges[:-1] + bin_duration_s // 2.

        # do alignment
        shift_indices, target_histogram, shift_covs_block = \
            iterative_template_registration(motion_histograms,
                                            non_rigid_windows=non_rigid_windows,
                                            num_shifts_global=method_kwargs['num_shifts_global'],
                                            num_iterations=method_kwargs['num_iterations'],
                                            num_shifts_block=method_kwargs['num_shifts_block'],
                                            smoothing_sigma=method_kwargs['smoothing_sigma'],
                                            kriging_p=method_kwargs['kriging_p'],
                                            kriging_d=method_kwargs['kriging_d'])

        # convert to um
        motion = -(shift_indices * bin_um)

        if output_extra_check:
            extra_check['motion_histograms'] = motion_histograms
            extra_check['target_histogram'] = target_histogram
            extra_check['shift_covs_block'] = shift_covs_block

    if output_extra_check:
        extra_check['temporal_hist_bin_edges'] = temporal_hist_bin_edges
        extra_check['spatial_hist_bin_edges'] = spatial_hist_bin_edges

    # replace nan by zeros
    motion[np.isnan(motion)] = 0

    if clean_motion_kwargs is not None:
        motion = clean_motion_vector(motion, temporal_bins, bin_duration_s, **clean_motion_kwargs)

    
    if upsample_to_histogram_bin is None:
        upsample_to_histogram_bin = non_rigid_kwargs is not None
    
    if upsample_to_histogram_bin:
        # do upsample
        non_rigid_windows = np.array(non_rigid_windows)
        non_rigid_windows /= non_rigid_windows.sum(axis=0, keepdims=True)
        spatial_bins_non_rigid = spatial_bin_edges[:-1] + bin_um / 2
        motion = motion @ non_rigid_windows

    if output_extra_check:
        return motion, temporal_bins, spatial_bins_non_rigid, extra_check
    else:
        return motion, temporal_bins, spatial_bins_non_rigid


def get_spatial_bin_edges(recording, direction, margin_um, bin_um):
    # contact along one axis
    probe = recording.get_probe()
    dim = ['x', 'y', 'z'].index(direction)
    contact_pos = probe.contact_positions[:, dim]

    min_ = np.min(contact_pos) - margin_um
    max_ = np.max(contact_pos) + margin_um
    spatial_bins = np.arange(min_, max_+bin_um, bin_um)

    return spatial_bins


def make_2d_motion_histogram(recording, peaks, peak_locations,
                             weight_with_amplitude=False, direction='y',
                             bin_duration_s=1., bin_um=2., margin_um=50,
                             spatial_bin_edges=None):
    """
    Generate 2d motion histogram in depth and time.

    Parameters
    ----------
    recording : BaseRecording
        The input recording
    peaks : np.array
        The peaks array
    peak_locations : np.array
        Array with peak locations
    weight_with_amplitude : bool, optional
        If True, motion histogram is weighted by amplitudes, by default False
    direction : str, optional
        'x', 'y', 'z', by default 'y'
    bin_duration_s : float, optional
        The temporal bin duration in s, by default 1.
    bin_um : float, optional
        The spatial bin size in um, by default 2. Ignored if spatial_bin_edges is given.
    margin_um : float, optional
        The margin to add to the minimum and maximum positions before spatial binning, by default 50.
        Ignored if spatial_bin_edges is given.
    spatial_bin_edges : np.array, optional
        The pre-computed spatial bin edges, by default None

    Returns
    -------
    motion_histogram
        2d np.array with motion histogram (num_temporal_bins, num_spatial_bins)
    temporal_bin_edges
        1d array with temporal bin edges
    spatial_bin_edges
        1d array with spatial bin edges
    """
    fs = recording.get_sampling_frequency()
    num_samples = recording.get_num_samples(segment_index=0)
    bin_sample_size = int(bin_duration_s * fs)
    sample_bin_edges = np.arange(0, num_samples + bin_sample_size, bin_sample_size)
    temporal_bin_edges = sample_bin_edges / fs
    if spatial_bin_edges is None:
        spatial_bin_edges = get_spatial_bin_edges(recording, direction, margin_um, bin_um)

    arr = np.zeros((peaks.size, 2), dtype='float64')
    arr[:, 0] = peaks['sample_ind']
    arr[:, 1] = peak_locations[direction]

    if weight_with_amplitude:
        weights = np.abs(peaks['amplitude'])
    else:
        weights = None

    motion_histogram, edges = np.histogramdd(arr, bins=(sample_bin_edges, spatial_bin_edges), weights=weights)

    # average amplitude in each bin
    if weight_with_amplitude:
        bin_counts, _ = np.histogramdd(arr, bins=(sample_bin_edges, spatial_bin_edges))
        bin_counts[bin_counts == 0] = 1
        motion_histogram = motion_histogram / bin_counts

    return motion_histogram, temporal_bin_edges, spatial_bin_edges


def make_3d_motion_histograms(recording, peaks, peak_locations,
                              direction='y', bin_duration_s=1., bin_um=2.,
                              margin_um=50, num_amp_bins=20,
                              log_transform=True, spatial_bin_edges=None):
    """
    Generate 3d motion histograms in depth, amplitude, and time.
    This is used by the "iterative_template_registration" (Kilosort2.5) method.

    Parameters
    ----------
    recording : BaseRecording
        The input recording
    peaks : np.array
        The peaks array
    peak_locations : np.array
        Array with peak locations
    direction : str, optional
        'x', 'y', 'z', by default 'y'
    bin_duration_s : float, optional
        The temporal bin duration in s, by default 1.
    bin_um : float, optional
        The spatial bin size in um, by default 2. Ignored if spatial_bin_edges is given.
    margin_um : float, optional
        The margin to add to the minimum and maximum positions before spatial binning, by default 50.
        Ignored if spatial_bin_edges is given.
    log_transform : bool, optional
        If True, histograms are log-transformed, by default True
    spatial_bin_edges : np.array, optional
        The pre-computed spatial bin edges, by default None

    Returns
    -------
    motion_histograms
        3d np.array with motion histogram (num_temporal_bins, num_spatial_bins, num_amp_bins)
    temporal_bin_edges
        1d array with temporal bin edges
    spatial_bin_edges
        1d array with spatial bin edges
    """
    fs = recording.get_sampling_frequency()
    num_samples = recording.get_num_samples(segment_index=0)
    bin_sample_size = int(bin_duration_s * fs)
    sample_bin_edges = np.arange(0, num_samples+bin_sample_size, bin_sample_size)
    temporal_bin_edges = sample_bin_edges / fs
    if spatial_bin_edges is None:
        spatial_bin_edges = get_spatial_bin_edges(recording, direction, margin_um, bin_um)

    # pre-compute abs amplitude and ranges for scaling
    amplitude_bin_edges = np.linspace(0, 1, num_amp_bins + 1)
    abs_peaks = np.abs(peaks["amplitude"])
    max_peak_amp = np.max(abs_peaks)
    min_peak_amp = np.min(abs_peaks)
    # log amplitudes and scale between 0-1
    abs_peaks_log_norm = (np.log10(abs_peaks) - np.log10(min_peak_amp)) / \
        (np.log10(max_peak_amp) - np.log10(min_peak_amp))

    arr = np.zeros((peaks.size, 3), dtype='float64')
    arr[:, 0] = peaks['sample_ind']
    arr[:, 1] = peak_locations[direction]
    arr[:, 2] = abs_peaks_log_norm

    motion_histograms, edges = np.histogramdd(arr, bins=(sample_bin_edges, spatial_bin_edges, amplitude_bin_edges,))

    if log_transform:
        motion_histograms = np.log2(1 + motion_histograms)

    return motion_histograms, temporal_bin_edges, spatial_bin_edges


def compute_pairwise_displacement(motion_hist, bin_um, method='conv',
                                  weight_scale='linear', error_sigma=0.2,
                                  conv_engine='numpy', torch_device=None,
                                  batch_size=1, max_displacement_um=1500,
                                  corr_threshold=0, time_horizon_s=None,
                                  bin_duration_s=None, progress_bar=False): 
    """
    Compute pairwise displacement
    """
    from scipy import sparse
    assert conv_engine in ("torch", "numpy")
    size = motion_hist.shape[0]
    pairwise_displacement = np.zeros((size, size), dtype='float32')

    if time_horizon_s is not None:
        band_width = int(np.ceil(time_horizon_s / bin_duration_s))
    
    if conv_engine == 'torch':
        import torch
        if torch_device is None:
            torch_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if method == 'conv':
        if max_displacement_um is None:
            n = motion_hist.shape[1] // 2
        else:
            n = min(
                motion_hist.shape[1] // 2,
                int(np.ceil(max_displacement_um // bin_um)),
            )
        possible_displacement = np.arange(-n, n + 1) * bin_um

        conv_values = np.zeros((size, size), dtype='float32')
        xrange = trange if progress_bar else range

        motion_hist_engine = motion_hist
        if conv_engine == "torch":
            motion_hist_engine = torch.as_tensor(motion_hist, dtype=torch.float32, device=torch_device)

        if time_horizon_s is not None and time_horizon_s > 0:
            pairwise_displacement = sparse.dok_matrix((size, size), dtype=np.float32)
            correlation = sparse.dok_matrix((size, size), dtype=motion_hist.dtype)

            for i in xrange(size):
                hist_i = motion_hist_engine[None, i]
                pairwise_displacement[i, i] = 0
                correlation[i, i] = 1
                j_max = size if time_horizon_s is None else min(size, i + band_width)
                for j in range(i + 1, j_max):
                    corr = normxcorr1d(
                        hist_i,
                        motion_hist_engine[None, j],
                        padding=possible_displacement.size // 2,
                        conv_engine=conv_engine,
                    )
                    if conv_engine == "torch":
                        max_corr, ind_max = torch.max(corr, dim=2)
                        max_corr = max_corr.cpu()
                        ind_max = ind_max.cpu()
                    elif conv_engine == "numpy":
                        ind_max = np.argmax(corr, axis=2)
                        max_corr = corr[0, 0, ind_max]
                    if max_corr > corr_threshold:
                        pairwise_displacement[i, j] = -possible_displacement[ind_max]
                        pairwise_displacement[j, i] = possible_displacement[ind_max]
                        correlation[i, j] = correlation[j, i] = max_corr

            pairwise_displacement = pairwise_displacement.tocsr()
            correlation = correlation.tocsr()

        else:
            pairwise_displacement = np.empty((size, size), dtype=np.float32)
            correlation = np.empty((size, size), dtype=motion_hist.dtype)

            for i in xrange(0, size, batch_size):
                corr = normxcorr1d(
                    motion_hist_engine,
                    motion_hist_engine[i : i + batch_size],
                    padding=possible_displacement.size // 2,
                    conv_engine=conv_engine,
                )
                if conv_engine == "torch":
                    max_corr, best_disp_inds = torch.max(corr, dim=2)
                    best_disp = possible_displacement[best_disp_inds.cpu()]
                    pairwise_displacement[i : i + batch_size] = best_disp
                    correlation[i : i + batch_size] = max_corr.cpu()
                elif conv_engine == "numpy":
                    best_disp_inds = np.argmax(corr, axis=2)
                    max_corr = np.take_along_axis(corr, best_disp_inds[..., None], 2).squeeze()
                    best_disp = possible_displacement[best_disp_inds]
                    pairwise_displacement[i : i + batch_size] = best_disp
                    correlation[i : i + batch_size] = max_corr

            if corr_threshold > 0:
                which = correlation > corr_threshold
                pairwise_displacement *= which
                correlation *= which

    elif method == 'phase_cross_correlation':
        try:
            import skimage.registration
        except ImportError:
            raise ImportError("To use 'phase_cross_correlation' method install scikit-image")

        errors = np.zeros((size, size), dtype='float32')
        loop = range(size)
        if progress_bar:
            loop = tqdm(loop)
        for i in loop:
            for j in range(size):
                shift, error, diffphase = skimage.registration.phase_cross_correlation(motion_hist[i, :], 
                                                                                       motion_hist[j, :])
                pairwise_displacement[i, j] = shift * bin_um 
                errors[i, j] = error
        correlation = 1 - errors
        
    else:
        raise ValueError(f'method do not exists for compute_pairwise_displacement {method}')

    if weight_scale == 'linear':
        # between 0 and 1
        pairwise_displacement_weight = correlation
    elif weight_scale == 'exp':
        pairwise_displacement_weight = np.exp((correlation - 1) / error_sigma )

    return pairwise_displacement, pairwise_displacement_weight


def compute_global_displacement(
    pairwise_displacement,
    pairwise_displacement_weight=None,
    sparse_mask=None,
    convergence_method='gradient_descent',
    robust_regression_sigma=2,
    gradient_descent_max_iter=1000,
    lsqr_robust_n_iter=20,
    progress_bar=False,
):
    """
    Compute global displacement

    Reference:
    DECENTRALIZED MOTION INFERENCE AND REGISTRATION OF NEUROPIXEL DATA
    Erdem Varol1, Julien Boussard, Hyun Dong Lee

    Improved during Spike Sorting Hackathon 2022 by Erdem Varol and Charlie Windolf.
    
    This come from
    https://github.com/int-brain-lab/spikes_localization_registration/blob/main/registration_pipeline/image_based_motion_estimate.py#L211
    
    """
    size = pairwise_displacement.shape[0]

    if convergence_method == 'gradient_descent':
        from scipy.optimize import minimize
        from scipy.sparse import csr_matrix

        D = pairwise_displacement
        if pairwise_displacement_weight is not None or sparse_mask is not None:
            # weighted problem
            if pairwise_displacement_weight is None:
                pairwise_displacement_weight = np.ones_like(D)
            if sparse_mask is None:
                sparse_mask = np.ones_like(D)
            W = pairwise_displacement_weight * sparse_mask

            I, J = np.where(W > 0)
            Wij = W[I, J]
            Dij = D[I, J]
            W = csr_matrix((Wij, (I, J)), shape=W.shape)
            WD = csr_matrix((Wij * Dij, (I, J)), shape=W.shape)
            fixed_terms = (W @ WD).diagonal() - (WD @ W).diagonal()
            diag_WW = (W @ W).diagonal()
            Wsq = W.power(2)

            def obj(p):
                return 0.5 * np.square(Wij * (Dij - (p[I] - p[J]))).sum()

            def jac(p):
                return fixed_terms - 2 * (Wsq @ p) + 2 * p * diag_WW
        else:
            # unweighted problem, it's faster when we have no weights
            fixed_terms = -D.sum(axis=1) + D.sum(axis=0)

            def obj(p):
                v = np.square((D - (p[:, None] - p[None, :]))).sum()
                return 0.5 * v

            def jac(p):
                return fixed_terms + 2 * (size * p - p.sum())

        res = minimize(
            fun=obj, jac=jac, x0=D.mean(axis=1), method="L-BFGS-B"
        )
        if not res.success:
            print("Global displacement gradient descent had an error")
        displacement = res.x

    elif convergence_method == 'lsqr_robust':
        from scipy.sparse import csr_matrix
        from scipy.sparse.linalg import lsqr
        from scipy.stats import zscore

        if sparse_mask is not None:
            I, J = np.where(sparse_mask > 0)
        elif pairwise_displacement_weight is not None:
            I, J = pairwise_displacement_weight.nonzero()
        else:
            I, J = np.where(np.ones_like(pairwise_displacement, dtype=bool))

        nnz_ones = np.ones(I.shape[0], dtype=pairwise_displacement.dtype)

        if pairwise_displacement_weight is not None:
            if isinstance(pairwise_displacement_weight, scipy.sparse.csr_matrix):
                W = np.array(pairwise_displacement_weight[I, J]).T
            else:
                W = pairwise_displacement_weight[I, J][:,None]
        else:
            W = nnz_ones[:, None]
        if isinstance(pairwise_displacement, scipy.sparse.csr_matrix):
            V = np.array(pairwise_displacement[I, J])[0]
        else:
            V = pairwise_displacement[I, J]
        M = csr_matrix((nnz_ones, (range(I.shape[0]), I)))
        N = csr_matrix((nnz_ones, (range(I.shape[0]), J)))
        A = M - N
        idx = np.ones(A.shape[0], dtype=bool)

        xrange = trange if progress_bar else range
        for i in xrange(lsqr_robust_n_iter):
            p = lsqr(A[idx].multiply(W[idx]), V[idx] * W[idx][:,0])[0]
            idx = np.where(np.abs(zscore(A @ p - V)) <= robust_regression_sigma)
        displacement = p

    else:
        raise ValueError(f"Method {convergence_method} doesn't exists for compute_global_displacement")

    return displacement


def iterative_template_registration(spikecounts_hist_images,
                                    non_rigid_windows=None,
                                    num_shifts_global=15, num_iterations=10,
                                    non_rigid_window_overlap=0.5,
                                    num_shifts_block=5, smoothing_sigma=0.5,
                                    kriging_sigma=1, kriging_p=2, kriging_d=2):
    """
    Alignment function implemented by Kilosort2.5 and ported from pykilosort:
    https://github.com/int-brain-lab/pykilosort/blob/ibl_prod/pykilosort/datashift2.py#L166

    The main difference with respect to the original implementation are:
    * scipy is used for gaussian smoothing
    * windowing is implemented as tapering (instead of rectangular blocks)

    Modified by Alessio Buccino

    Parameters
    ----------
    spikecounts_hist_images : np.ndarray
        Spike count histogram images (num_temporal_bins, num_spatial_bins, num_amps_bins)
    non_rigid_windows : list, optional
        If num_non_rigid_windows > 1, this argument is required and it is a list of windows to 
        taper spatial bins in different blocks, by default None
    num_shifts_global : int, optional
        Number of spatial bin shifts to consider for global alignment, by default 15
    num_iterations : int, optional
        Number of iterations for global alignment procedure, by default 10
    non_rigid_window_overlap : float, optional
        Amount of overlap (between 0 and 1) between non-rigid windows, by default 0.5
    num_shifts_block : int, optional
        Number of spatial bin shifts to consider for non-rigid alignment, by default 5
    smoothing_sigma : float, optional
        Sigma of gaussian for covariance matrices smoothing, by default 0.5
    kriging_sogma : float, optional
        sigma parameter for kriging_kernel function
    kriging_p : float, optional
        p parameter for kriging_kernel function
    kriging_d : float, optional
        d parameter for kriging_kernel function

    Returns
    -------
    optimal_shift_indices
        Optimal shifts for each temporal and spatial bin (num_temporal_bins, num_non_rigid_windows)
    target_spikecount_hist
        Target histogram used for alignment (num_spatial_bins, num_amps_bins)
    """
    from scipy.ndimage import gaussian_filter, gaussian_filter1d

    assert 0 <= non_rigid_window_overlap <= 1, "'non_rigid_window_overlap' can be between 0 and 1!"
    # F is y bins by amp bins by batches
    # ysamp are the coordinates of the y bins in um
    spikecounts_hist_images = spikecounts_hist_images.swapaxes(0, 1).swapaxes(1, 2)
    num_temporal_bins = spikecounts_hist_images.shape[2]

    # look up and down this many y bins to find best alignment
    shift_covs = np.zeros((2 * num_shifts_global + 1, num_temporal_bins))
    shifts = np.arange(-num_shifts_global, num_shifts_global + 1)

    # mean subtraction to compute covariance
    F = spikecounts_hist_images
    Fg = F - np.mean(F, axis=0)

    # initialize the target "frame" for alignment with a single sample
    # here we removed min(299, ...)
    F0 = Fg[:, :, np.floor(num_temporal_bins / 2).astype("int") - 1]
    F0 = F0[:, :, np.newaxis]

    # first we do rigid registration by integer shifts
    # everything is iteratively aligned until most of the shifts become 0.
    best_shifts = np.zeros((num_iterations, num_temporal_bins))
    for iteration in range(num_iterations):
        for t, shift in enumerate(shifts):
            # for each NEW potential shift, estimate covariance
            Fs = np.roll(Fg, shift, axis=0)
            shift_covs[t, :] = np.mean(Fs * F0, axis=(0, 1))
        if iteration + 1 < num_iterations:
            # estimate the best shifts
            imax = np.argmax(shift_covs, axis=0)
            # align the data by these integer shifts
            for t, shift in enumerate(shifts):
                ibest = imax == t
                Fg[:, :, ibest] = np.roll(Fg[:, :, ibest], shift, axis=0)
                best_shifts[iteration, ibest] = shift
            # new target frame based on our current best alignment
            F0 = np.mean(Fg, axis=2)[:, :, np.newaxis]
    target_spikecount_hist = F0[:, :, 0]

    # now we figure out how to split the probe into nblocks pieces
    # if len(non_rigid_windows) = 1, then we're doing rigid registration
    num_non_rigid_windows = len(non_rigid_windows)

    # for each small block, we only look up and down this many samples to find
    # nonrigid shift
    shifts_block = np.arange(-num_shifts_block, num_shifts_block + 1)
    num_shifts = len(shifts_block)
    shift_covs_block = np.zeros((2 * num_shifts_block + 1, num_temporal_bins, num_non_rigid_windows))

    # this part determines the up/down covariance for each block without
    # shifting anything
    for window_index in range(num_non_rigid_windows):
        win = non_rigid_windows[window_index]
        window_slice = np.flatnonzero(win > 1e-5)
        window_slice = slice(window_slice[0], window_slice[-1])
        tiled_window = win[window_slice, np.newaxis, np.newaxis]
        Ftaper = Fg[window_slice] * np.tile(tiled_window, (1,) + Fg.shape[1:])
        for t, shift in enumerate(shifts_block):
            Fs = np.roll(Ftaper, shift, axis=0)
            F0taper = F0[window_slice] * np.tile(tiled_window, (1,) + F0.shape[1:])
            shift_covs_block[t, :, window_index] = np.mean(Fs * F0taper, axis=(0, 1))

    # gaussian smoothing:
    # here the original my_conv2_cpu is substituted with scipy gaussian_filters
    shift_covs_block_smooth = shift_covs_block.copy()
    shifts_block_up = np.linspace(-num_shifts_block, num_shifts_block,
                                  (2 * num_shifts_block * 10) + 1)
    # 1. 2d smoothing over time and blocks dimensions for each shift
    for shift_index in range(num_shifts):
        shift_covs_block_smooth[shift_index, :, :] = gaussian_filter(
            shift_covs_block_smooth[shift_index, :, :], smoothing_sigma
        )  # some additional smoothing for robustness, across all dimensions
    # 2. 1d smoothing over shift dimension for each spatial block
    for window_index in range(num_non_rigid_windows):
        shift_covs_block_smooth[:, :, window_index] = gaussian_filter1d(
            shift_covs_block_smooth[:, :, window_index], smoothing_sigma, axis=0
        )  # some additional smoothing for robustness, across all dimensions
    upsample_kernel = kriging_kernel(shifts_block[:, np.newaxis],
                                     shifts_block_up[:, np.newaxis],
                                     sigma=kriging_sigma, p=kriging_p, d=kriging_d)

    optimal_shift_indices = np.zeros((num_temporal_bins, num_non_rigid_windows))
    for window_index in range(num_non_rigid_windows):
        # using the upsampling kernel K, get the upsampled cross-correlation
        # curves
        upsampled_cov = upsample_kernel.T @ shift_covs_block_smooth[:, :, window_index]

        # find the max index of these curves
        imax = np.argmax(upsampled_cov, axis=0)

        # add the value of the shift to the last row of the matrix of shifts
        # (as if it was the last iteration of the main rigid loop )
        best_shifts[num_iterations - 1, :] = shifts_block_up[imax]

        # the sum of all the shifts equals the final shifts for this block
        optimal_shift_indices[:, window_index] = np.sum(best_shifts, axis=0)

    return optimal_shift_indices, target_spikecount_hist, shift_covs_block


def normxcorr1d(template, x, padding="same", conv_engine="torch"):
    """normxcorr1d: 1-D normalized cross-correlation

    Returns the cross-correlation of `template` and `x` at spatial lags
    determined by `mode`. Useful for estimating the location of `template`
    within `x`.
    This might not be the most efficient implementation -- ideas welcome.
    It uses a direct convolutional translation of the formula
        corr = (E[XY] - EX EY) / sqrt(var X * var Y)

    Arguments
    ---------
    template : tensor, shape (num_templates, length)
        The reference template signal
    x : tensor, 1d shape (length,) or 2d shape (num_inputs, length)
        The signal in which to find `template`
    padding : int, optional
        How far to look? if unset, we'll use half the length
    assume_centered : bool
        Avoid a copy if your data is centered already.

    Returns
    -------
    corr : tensor
    """
    if conv_engine == "torch":
        import torch
        import torch.nn.functional as F
        conv1d = F.conv1d
        npx = torch
    elif conv_engine == "numpy":
        conv1d = scipy_conv1d
        npx = np
    else:
        raise ValueError(f"Unknown conv_engine {conv_engine}")

    x = npx.atleast_2d(x)
    num_templates, length = template.shape
    num_inputs, length_ = template.shape
    assert length == length_

    # compute expectations
    if conv_engine == "torch":
        ones = npx.ones((1, 1, length), dtype=x.dtype, device=x.device)
    else:
        ones = npx.ones((1, 1, length), dtype=x.dtype)
    # how many points in each window? seems necessary to normalize
    # for numerical stability.
    N = conv1d(ones, ones, padding=padding)
    Et = conv1d(ones, template[:, None, :], padding=padding) / N
    Ex = conv1d(x[:, None, :], ones, padding=padding) / N

    # compute covariance
    corr = conv1d(x[:, None, :], template[:, None, :], padding=padding) / N
    corr -= Ex * Et

    # compute variances for denominator, using var X = E[X^2] - (EX)^2
    var_template = conv1d(
        ones, npx.square(template)[:, None, :], padding=padding
    )
    var_template = var_template / N - npx.square(Et)
    var_x = conv1d(
        npx.square(x)[:, None, :], ones, padding=padding
    )
    var_x = var_x / N - npx.square(Ex)

    # now find the final normxcorr and get rid of NaNs in zero-variance areas
    corr /= npx.sqrt(var_x * var_template)
    corr[~npx.isfinite(corr)] = 0

    return corr


def scipy_conv1d(input, weights, padding="valid"):
    """SciPy translation of torch F.conv1d"""
    from scipy.signal import correlate

    n, c_in, length = input.shape
    c_out, in_by_groups, kernel_size = weights.shape
    assert in_by_groups == c_in == 1

    if padding == "same":
        mode = "same"
        length_out = length
    elif padding == "valid":
        mode = "valid"
        length_out = length - 2 * (kernel_size // 2)
    elif isinstance(padding, int):
        mode = "valid"
        input = np.pad(input, [*[(0,0)] * (input.ndim - 1), (padding, padding)])
        length_out = length - (kernel_size - 1) + 2 * padding
    else:
        raise ValueError(f"Unknown padding {padding}")

    output = np.zeros((n, c_out, length_out), dtype=input.dtype)
    for m in range(n):
        for c in range(c_out):
            output[m, c] = correlate(input[m, 0], weights[c, 0], mode=mode)

    return output


def clean_motion_vector(motion, temporal_bins, bin_duration_s, 
                        speed_threshold=30, sigma_smooth_s=None):
    """
    Simple machinery to remove spurious fast bump in the motion vector.
    Also can applyt a smoothing.


    Arguments
    ---------
    motion: numpy array 2d
        Motion estimate in um.
    temporal_bins: numpy.array 1d
        temporal bins (bin center)
    bin_duration_s: float
        bin duration in second
    speed_threshold: float (units um/s)
        Maximum speed treshold between 2 bins allowed.
        Expressed in um/s
    sigma_smooth_s: None or float
        Optional smooting gaussian kernel.

    Returns
    -------
    corr : tensor


    """
    motion_clean = motion.copy()
    
    # STEP 1 : 
    #   * detect long plateau or small peak corssing the speed thresh
    #   * mask the period and interpolate
    for i in range(motion.shape[1]):
        one_motion = motion_clean[:, i]
        speed = np.diff(one_motion, axis=0) / bin_duration_s
        inds,  = np.nonzero(np.abs(speed) > speed_threshold)
        inds +=1 
        if inds.size % 2 == 1:
            # more compicated case: number of of inds is odd must remove first or last
            # take the smallest duration sum
            inds0 = inds[:-1]
            inds1 = inds[1:]
            d0 = np.sum(inds0[1::2] - inds0[::2])
            d1 = np.sum(inds1[1::2] - inds1[::2])
            if d0 < d1:
                inds = inds0
        mask = np.ones(motion_clean.shape[0], dtype='bool')
        for i in range(inds.size // 2):
            mask[inds[i*2]:inds[i*2+1]] = False
        f = scipy.interpolate.interp1d(temporal_bins[mask], one_motion[mask])
        one_motion[~mask] = f(temporal_bins[~mask])
    
    # Step 2 : gaussian smooth
    if sigma_smooth_s is not None:
        half_size = motion_clean.shape[0] // 2
        if motion_clean.shape[0] % 2 == 0:
            # take care of the shift
            bins = (np.arange(motion_clean.shape[0]) - half_size + 1) * bin_duration_s
        else:
            bins = (np.arange(motion_clean.shape[0]) - half_size) * bin_duration_s
        smooth_kernel = np.exp( -bins**2 / ( 2 * sigma_smooth_s **2))
        smooth_kernel /= np.sum(smooth_kernel)
        smooth_kernel = smooth_kernel[:, None]
        motion_clean = scipy.signal.fftconvolve(motion_clean, smooth_kernel, mode='same', axes=0)
    
    return motion_clean


def kriging_kernel(source_location, target_location, sigma=1, p=2, d=2):
    from scipy.spatial.distance import cdist
    dist_xy = cdist(source_location, target_location, metric='euclidean')
    K = np.exp(-(dist_xy / sigma)**p / d)
    return K
