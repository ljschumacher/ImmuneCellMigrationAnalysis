import sys
sys.path.append('/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/')

import numpy as np
from utils.angles import angle_between
from in_silico.walkers import reference_axis
from utils.distributions import WrappedNormal, Uniform
from in_silico.sources import Source
from inference.base import MCMC
from utils.checks import check_valid_prior, check_is_numpy
from utils.misc import nan_concatenate

def prepare_paths(paths: list, min_t: int=5) -> np.ndarray:
    """
    This function converts a list of paths, which come in the standard form, (each element is a (t, 3) array
    where column 0 has the frame index, column 1 has the x coordinates and column 2 has the y coordinates) into
    a single array that can be used by the MCMC inference class. This output array will have shape
    (longest path, 2, number of paths). Any path that is shorter than the longest path will have its bottom
    values in this array filled with nans.

    [ array((7, 3)), array((9, 3)), array((5, 3)) ... ] -->

       ---  ---  ---
    [ ---  ---  ---
       |    |    |
       |    |  path3
     path1  |    |--
       |  path2 ---
       |--  |   nan   ...
      ---   |   nan
      nan   |-- nan
      nan  ---  nan
      nan  nan  nan


    Parameters
    ----------
    paths           A regular paths object: a list of numpy arrays f shape (t, 3) holding the t-x-y coordinates of
                    that particular path

    min_t           The minimum number of steps a path should have in  order to be included in the matrix. Setting
                    this to a higher value speeds up inference a lot as the matrix has far fewer columns.

    Returns
    -------
    paths_array     A numpy array formatting of the paths ready to be
                    passed to the inference class. (T, 2, N)
    """


    if len(paths) == 0:
        return np.array([]).reshape(0, 2, 0)

    max_t = max(paths, key=lambda x: x.shape[0]).shape[0]
    paths = [path[:, 1:] for path in paths if path.shape[0] >= min_t]

    paths_array = np.zeros((max_t, 2, len(paths)))
    paths_array[:] = np.nan

    for i, path in enumerate(paths):
        paths_array[:path.shape[0], :, i] = path

    return paths_array


def get_alphas_betas(paths: np.ndarray, source: Source):
    """
    Given a set of raw x-y coordinates, get a list of the alphas and betas

    Params:

        paths:     np.array - (T, 2, N) - paths of N walkers walking for T timesteps

    Returns:

        alphas:    np.array - (T-1, N): angle taken at each time step
        betas:     np.array - (T-1, N): direction to source at each time step

    """

    T, _, N = paths.shape

    if T <= 1 or N == 0:
        return np.array([]).reshape(0, 0), np.array([]).reshape(0, 0)

    moves = paths[1:, :, :] - paths[:-1, :, :]
    alphas = np.apply_along_axis(lambda move: angle_between(reference_axis, move), 1, moves)
    directions_to_source = np.apply_along_axis(source.direction_to_source, 1, paths)
    betas = np.apply_along_axis(lambda d: angle_between(reference_axis, d), 1, directions_to_source)[:-1, :]

    return alphas, betas


class BiasedPersistentInfernce(MCMC):

    def __init__(self, paths: list, sources: list, priors: list=None):
        """
        Initialise an inference pipeline for biased persistent walkers.
        Pass in a set of observed paths, with a corresponding set of
        sources.

        Parameters
        ----------
        paths       a list of np.arrays of shape (T+1, 2, N).These are the paths of N
                    walkers walking for T timesteps. Not every path must be of the same
                    length, but the longest should be T+1. For every other path i, start
                    at index 0 and fill up to length t [:t, :, i] leaving [t:, :, i]
                    filled with np.nan values. Can also be a singe numpy array for one
                    set of paths only.

        source      a list of sources.Source objects, which correspond to the paths. Can
                    also be a single Source, if a single path is passed

        """

        super().__init__()

        # perform checks on the paths and sources
        if isinstance(paths, np.ndarray) and isinstance(sources, Source):
            paths = [paths]
            sources = [sources]
        else:
            assert isinstance(paths, (list, tuple))
            assert isinstance(sources, (list, tuple))

        # perform prior checks
        if priors is None:
            self.priors = [Uniform(0, 1), Uniform(0, 1), Uniform(0, 1)]
            self.uniform_prior = True

        else:
            self.uniform_prior = False
            if isinstance(priors, list):
                if check_valid_prior(priors):
                    self.priors = priors
            else:
                raise TypeError('Priors must be a list of 3 distribution objects')

        # get a list of alphas and betas for each set of paths
        alphas = []
        betas = []
        for path, source in zip(paths, sources):
            a, b = get_alphas_betas(path, source)
            if a.shape != (0, 0) and b.shape != (0, 0):
                alphas.append(a)
                betas.append(b)

        # concantenate them into two big arrays
        alphas = nan_concatenate(alphas, axis=1)
        betas = nan_concatenate(betas, axis=1)

        # extract initial angles
        self.alpha0 = alphas[0, :]
        self.beta0 = betas[0, :]

        # seperate angles and previous angles
        self.betas = betas[1:, :]
        self.alphas = alphas[1:, :]
        self.alphas_ = alphas[:-1, :]

    def log_likelihood(self, params: np.ndarray) -> float:
        """
        Get the log-likelihood of a set of parameters w, p, b.

        * 'alpha' refers to the current observed angle under consideration.
        * 'beta' refers to the current angle towards the source
        * 'alpha_' refers to the previous observed angle

        at any step t > 1, the probability distribution governing the observed
        angle alpha is

             p(alpha) = w * WN(alpha; beta, -2log(b)) + (1-w) * WN(alpha; alpha_, -2log(p))

        where WN(mu, sig) is a wrapped normal distribution. The log likelihood
        of observing T strings of angles 'alphas' is

            SUM_t SUM_i log[p(alpha_ti)]

        Params:

            w:         The variable w which defines the probability of taking a biased step
            p:         The variable p, defining the variance of persistent motion
            b:         The variable b, defining the variance of boased motion

        Returns:

            log_prob:  The log probability that the paths were generated by a walker
                       with parameters (w, p, b)
        """

        if (params > 1).any() or (params < 0).any():
            return -np.inf

        w, p, b = params

        sig_b = -2 * np.log(b)
        sig_p = -2 * np.log(p)

        # The probability of observing the first step, given the angle beta0 towards the source
        p_0 = WrappedNormal(mu=self.beta0, sig=sig_b).pdf(x=self.alpha0)

        # biased probabilities
        p_b = WrappedNormal(mu=self.betas, sig=sig_b).pdf(x=self.alphas)

        # persistent probabilities
        p_p = WrappedNormal(mu=self.alphas_, sig=sig_p).pdf(x=self.alphas)

        # combined probabilities
        p_t = w * p_b + (1 - w) * p_p

        # take logs
        log_p_0 = np.log(p_0)
        log_p_t = np.log(p_t)

        # this handles for when each path has an uneven number of steps. Some values will be nan
        log_p_t[np.isnan(log_p_t)] = 0
        log_p_0[np.isnan(log_p_0)] = 0

        return log_p_0.sum() + log_p_t.sum()

    def log_prior(self, params: np.ndarray) -> float:
        """
        For a given set of parameters, calculate the log of the prior
        pdf at this value. If our prior is Uniform(0, 1) for each
        dimension, then the log prior is 0 if all params are between
        0 and 1, and -inf if any param is out of that range.

        Parameters
        ----------
        params      Numpy array of shape (3,) containing the paramater
                    values

        Returns
        -------
        a float value of the log prior at the params value

        """


        if self.uniform_prior:

            if (params > 1).any() or (params < 0).any():
                return -np.inf
            else:
                return 0

        else:
            return sum([prior.logpdf(param) for prior, param in zip(self.priors, params)])

if __name__ == '__main__':

    from in_silico.walkers import BP_Leukocyte
    from in_silico.sources import PointSource
    from utils.plotting import plot_wpb_dist
    from in_vivo.image_analysis import CellTracker

    if False:
        source1 = PointSource(position=np.array([0, 0]))
        walker1 = BP_Leukocyte(np.array([0.5, 0.7, 0.8]), source1)
        paths1 = walker1.walk(np.random.uniform(-5, 5, size=(20, 2)), T=100)

        source2 = PointSource(position=np.array([3, 3]))
        walker2 = BP_Leukocyte(np.array([0.5, 0.7, 0.8]), source2)
        paths2 = walker2.walk(np.random.uniform(-5, 5, size=(20, 2)), T=80)

        source3 = PointSource(position=np.array([-2, -3]))
        walker3 = BP_Leukocyte(np.array([0.5, 0.7, 0.8]), source3)
        paths3 = walker3.walk(np.random.uniform(-5, 5, size=(20, 2)), T=120)

        inferer = BiasedPersistentInfernce([paths1, paths2, paths3], [source1, source2, source3])
        out = inferer.multi_infer(init_params=np.random.uniform(0, 1, size=(5, 3)), n_steps=10000, burn_in=3000, suppress_warnings=True)

        plot_wpb_dist(out)

    if True:

        # from in_vivo.set_source import set_source
        # from skimage.io import imread
        # from in_vivo.image_analysis import detect_all_cells, find_paths, paths_to_array, link_paths

        my_csvs = ['/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/2 hour wound 1 Spots in tracks statistics.csv',
                   '/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/2 hour wound 2 Spots in tracks statistics.csv',
                   '/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/2 hour wound 3 Spots in tracks statistics.csv',
                   '/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/control 1 hour wound Spots in tracks statistics.csv']

        their_csvs = ['/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/MAX 15.12.18 ubi ecadGFPsrpGFP srp3xmch x white.mvd2.csv',
                      '/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/MAX_15.12.18 ubi ecadGFPsrpGFP srp3xmch x white.mvd2 2.csv',
                      '/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/MAX_15.12.18 ubi ecadGFPsrpGFP srp3xmch x white.mvd2 3.csv',
                      '/home/ed/Documents/Academic/Edinburgh/Courses/Dissertation/LeukocyteMigration/in_vivo/Pupae 1 concatenated ubi-ecad-GFP, srpGFP; srp-3xH2Amch x white-1HR.csv']

        tif_files = ['/media/ed/DATA/Datasets/Leukocytes/control wounded 2hr/Wound 1/MAX 15.12.18 ubi ecadGFPsrpGFP srp3xmch x white.mvd2.tif',
                     '/media/ed/DATA/Datasets/Leukocytes/control wounded 2hr/Wound 2/MAX_15.12.18 ubi ecadGFPsrpGFP srp3xmch x white.mvd2 2.tif',
                     '/media/ed/DATA/Datasets/Leukocytes/control wounded 2hr/Wound 3/MAX_15.12.18 ubi ecadGFPsrpGFP srp3xmch x white.mvd2 3.tif',
                     '/media/ed/DATA/Datasets/Leukocytes/Control wounded 1hr/Pupae 1 concatenated ubi-ecad-GFP, srpGFP; srp-3xH2Amch x white-1HR.tif']

        channels = [1, 1, 1, 0]
        # for tif, c in zip(tif_files, channels):
        #     frames = imread(tif)[:, c, :, :]
        #     set_source(frames)

        sources = [PointSource(position=np.array([431.24, 387.06])),
                   PointSource(position=np.array([312.85, 437.80])),
                   PointSource(position=np.array([404.66, 449.88])),
                   PointSource(position=np.array([382.91, 365.32]))]

        tracker = CellTracker(tif_file=tif_files, color_channel=channels)
        tracker.compute_paths(step_size=10, detector='DoG')
        segmented_paths = tracker.segment_paths(source=sources)

        bins = list(segmented_paths.keys())

        for bin in bins:

            inferer = BiasedPersistentInfernce(*segmented_paths[bin])
            out = inferer.multi_infer(init_params=np.random.uniform(0, 1, size=(5, 3)), n_steps=10000, burn_in=3000, suppress_warnings=True)
            plot_wpb_dist(out, title=str(bin))

        # for tif, source, c in zip(tif_files, sources, channels):
        #
        #     frames = imread(tif)[:, c, :, :]
        #     T, Y, X = frames.shape
        #     cells = detect_all_cells(frames)
        #     paths = find_paths(cells, X, Y)
        #     array = paths_to_array(paths)

