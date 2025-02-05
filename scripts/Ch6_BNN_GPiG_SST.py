"""
SST data implementation of GPi-G BNN prior and posterior
"""

import torch
import numpy as np
import matplotlib as mpl
import matplotlib.pylab as plt
import os, sys
import pickle
from scipy.io import netcdf

CWD = sys.path[0]  # directory containing script
OUT_DIR = os.path.join(CWD, "sst_output")
FIG_DIR = os.path.join(CWD, "sst_figures")
DATA_DIR = os.path.join(CWD, "data")
sys.path.append(os.path.dirname(CWD))

from bnn_spatial.bnn.nets import GaussianNet, BlankNet
from bnn_spatial.bnn.layers.embedding_layer import EmbeddingLayer
from bnn_spatial.gp.model import GP
from bnn_spatial.gp import base, kernels
from bnn_spatial.utils import util
from bnn_spatial.utils.rand_generators import GridGenerator
from bnn_spatial.utils.plotting import plot_param_traces, plot_output_traces, plot_output_hist, plot_output_acf, \
    plot_mean_sd, plot_rbf, plot_samples_2d, plot_lipschitz, plot_cov_nonstat, plot_cov_contours, plot_bnn_grid
from bnn_spatial.stage1.wasserstein_mapper import MapperWasserstein
from bnn_spatial.stage2.likelihoods import LikGaussian
from bnn_spatial.stage2.priors import FixedGaussianPrior, OptimGaussianPrior
from bnn_spatial.stage2.bayes_net import BayesNet
from bnn_spatial.metrics.sampling import compute_rhat
from bnn_spatial.sst.sst_generator import SST
from bnn_spatial.metrics.prediction import rmspe, perc_coverage, interval_score
from scipy.optimize import minimize, basinhopping

os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'  # for handling OMP: Error 15 (2022/11/21)

#plt.rcParams['figure.figsize'] = [14, 7]
plt.rcParams['figure.constrained_layout.use'] = True
plt.rcParams['image.aspect'] = 'auto'
plt.rcParams['font.size'] = 24
plt.rcParams['axes.titlesize'] = 24
#mpl.use('TkAgg')
#mpl.rcParams['text.usetex'] = True
#mpl.rcParams['text.latex.preamble'] = [r'\usepackage{amsmath}']

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')  # set device to GPU if available

# Ensure directories exist (otherwise error occurs)
util.ensure_dir(OUT_DIR)
util.ensure_dir(FIG_DIR)
stage1_file = open(OUT_DIR + "/stage1.file", "wb")
stage1_txt = open(FIG_DIR + "/stage1.txt", "w+")
stage2_file = open(OUT_DIR + "/stage2.file", "wb")
stage2_txt = open(FIG_DIR + "/stage2.txt", "w+")

# Set seed (make results replicable)
util.set_seed(1)

# Specify BNN structure
hidden_dims = [9**2] + [40] * 10  # list of hidden layer dimensions
depth = len(hidden_dims)
embed_width = hidden_dims[0]
transfer_fn = "tanh"  # activation function

to_save = {'hidden_dims': hidden_dims,
           'n_hidden_layers': depth,
           'transfer_fn': transfer_fn}
pickle.dump(to_save, stage1_file)

"""
Fixed BNN prior (non-optimised)
"""

n_test = 64  # width/height of input matrix
test_range = np.linspace(0, 1, n_test)  # standardised range of x1 and x2 coordinates
X1, X2 = np.meshgrid(test_range, test_range)  # Cartesian indexing (xy) is default
test_array = np.vstack((X1.flatten(), X2.flatten())).T  # one col for each of X1 and X2, shape (n_test**2, 2)
test_tensor = torch.from_numpy(test_array).to(device).float()
n_plot = 512  # number of neural network samples to use in plots

# Note: the flatten operation (row-wise, default C-style) can be reversed by .reshape(n_test, -1)

# Embedding layer and RBF evaluations
rbf_ls = 0.14  # choose appropriately for the test range
embedding_layer = EmbeddingLayer(input_dim=2,
                                 output_dim=embed_width,
                                 domain=test_tensor,
                                 rbf_ls=rbf_ls)
rbf = embedding_layer(test_tensor)

# Plot the RBFs used in the embedding layer
plot_rbf(domain=test_array,
         net_width=embed_width,
         lenscale=rbf_ls,
         levels=[0.78])
plt.savefig(FIG_DIR + '/embedding_RBFs_lenscale{}.png'.format(rbf_ls), bbox_inches='tight')

to_save = {'n_test': n_test,
           'n_plot': n_plot,
           'rbf_ls': rbf_ls}
pickle.dump(to_save, stage1_file)

# Initialize fixed BNN
std_bnn = GaussianNet(input_dim=2,
                      output_dim=1,
                      activation_fn=transfer_fn,
                      hidden_dims=hidden_dims,
                      prior_per='layer').to(device)

# Perform predictions using n_plot sets of sampled weights/biases
# std_bnn_samples = std_bnn.sample_functions(test_tensor, n_plot).detach().cpu().numpy().squeeze()
std_bnn_samples = np.array([std_bnn(test_tensor).detach().cpu().numpy() for _ in range(n_plot)]).squeeze().T
# ensure shape of tensor is (n_test ** 2, n_plot); rows for inputs, cols for samples

# Resize array of predictions
std_bnn_samples_ = np.zeros((n_plot, n_test, n_test))
for ss in range(n_plot):
    sample = std_bnn_samples[:, ss].T
    sample_ = sample.reshape(n_test, -1)
    std_bnn_samples_[ss, :, :] = sample_

# Obtain mean and std dev of samples
std_bnn_mean = np.mean(std_bnn_samples_, axis=0)
std_bnn_sd = np.std(std_bnn_samples_, axis=0)

# Plot 4-by-4 panels of samples
plot_samples_2d(samples=std_bnn_samples_,
                extent=[0, 1],
                figsize=(13.75,12))
plt.savefig(FIG_DIR + '/std_prior_samples.png', bbox_inches='tight')

"""
Use SST data as target prior samples
"""

# Extract SST data from netcdf file
file2read = netcdf.NetCDFFile(DATA_DIR + "/global-analysis-forecast-phy-001-024_1551608429013.nc", "r", mmap=False)
temp = file2read.variables["thetao"]  # sea-water potential temperature (SST), raw values are integers
lon = file2read.variables["longitude"]
lat = file2read.variables["latitude"]
time = file2read.variables["time"]
file2read.close()

# Examine dimensions of SST data array
print('SST data array dimensions: time {}, depth {}, lat {}, lon {}'
      .format(temp.data.shape[0], temp.data.shape[1], temp.data.shape[2], temp.data.shape[3]))

# Obtain sea-surface temperatures in celsius
tempc = temp.data * temp.scale_factor + temp.add_offset

# Instantiate SST data generator, and generate data for sampling
print('Generating SST data')
sst = SST(sst_data=tempc)
sst.generate_data()

# Sample flattened SST data panels
sst_samples = sst.sample_functions(n_plot + 1)

# Resize array of predictions
sst_samples_ = np.zeros((n_plot, n_test, n_test))
for ss in range(n_plot + 1):
    sample = sst_samples[:, ss].T
    sample_ = sample.reshape(n_test, -1)
    if ss < n_plot:
        sst_samples_[ss, :, :] = sample_

# Use the final sample as the latent function (simulated from GP)
sst_latent = sample
sst_latent_ = sample_

# Obtain mean and std dev of samples
sst_mean = np.mean(sst_samples_, axis=0)
sst_sd = np.std(sst_samples_, axis=0)

# Plot 4-by-4 panels of samples
plot_samples_2d(samples=sst_samples_,
                extent=[0, 1],
                figsize=(13.75,12))
plt.savefig(FIG_DIR + '/SST_prior_samples.png', bbox_inches='tight')

"""
SST-induced BNN prior
"""

# Initialize BNN to be optimised
opt_bnn = GaussianNet(input_dim=2,
                      output_dim=1,
                      activation_fn=transfer_fn,
                      hidden_dims=hidden_dims,
                      domain=test_tensor,
                      fit_means=True,
                      prior_per='parameter',
                      nonstationary=True).to(device)

# Use a grid of `n_data` points for the measurement set (`n_data` specified in MapperWasserstein)
data_generator = GridGenerator(0, 1, input_dim=2)

# Perform Wasserstein optimisation to match BNN prior (opt_bnn) with GP prior
mapper_num_iters = 1000
outer_lr = 0.002  # initial learning rate for outer optimiser (wrt psi)
inner_lr = 0.01  # initial learning rate for inner optimiser (wrt theta)
inner_steps = 20  # number of steps for inner optimisation loop
n_measure = n_test**2  # measurement set size
n_stochastic = 64  # number of BNN/GP samples in Wasserstein algorithm
starting_steps = 1000  # number of inner optimisation steps at first iteration (optional)
starting_lr = 0.0003  # initial learning rate for first inner optimisation run

to_save = {'mapper_num_iters': mapper_num_iters,
           'outer_lr': outer_lr,
           'inner_lr': inner_lr,
           'inner_steps': inner_steps,
           'n_measure': n_measure,
           'n_stochastic': n_stochastic}
pickle.dump(to_save, stage1_file)

if not os.path.exists(os.path.join(OUT_DIR, "ckpts/it-{}.ckpt".format(mapper_num_iters))):
    mapper = MapperWasserstein(sst, opt_bnn, data_generator,
                               out_dir=OUT_DIR,
                               wasserstein_steps=inner_steps,
                               wasserstein_lr=inner_lr,
                               starting_steps=starting_steps,
                               starting_lr=starting_lr,
                               n_data=n_measure,
                               n_gpu=1,
                               gpu_gp=True,
                               save_memory=True,
                               raw_data=True,
                               continue_training=True)
    w_hist = mapper.optimise(num_iters=mapper_num_iters,
                             n_samples=n_stochastic,  # originally 512
                             lr=outer_lr,
                             print_every=10)
    path = os.path.join(OUT_DIR, "wsr_values.log")
    np.savetxt(path, w_hist, fmt='%.6e')

# Obtain Wasserstein distance values for each iteration
wdist_file = os.path.join(OUT_DIR, "wsr_values.log")
wdist_vals = np.loadtxt(wdist_file)
indices = np.arange(mapper_num_iters)[::1]  # indices for use in plots (::step)

# Plot Wasserstein iterations
plt.figure()
plt.plot(indices, wdist_vals[indices], "-ko", ms=4)
plt.ylabel(r'$W_1(p_{gp}, p_{nn})$')
plt.xlabel('Iteration (Outer Loop)')
plt.savefig(FIG_DIR + '/Wasserstein_steps.png', bbox_inches='tight')

# Plot Wasserstein iterations on log scale
plt.figure()
plt.plot(indices, wdist_vals[indices], "-ko", ms=4)
plt.yscale('log')
plt.ylabel(r'$W_1(p_{gp}, p_{nn})$')
plt.xlabel('Iteration (Outer Loop)')
plt.savefig(FIG_DIR + '/Wasserstein_steps_log.png', bbox_inches='tight')

# Obtain NN gradient norms appearing in Lipschitz loss penalty term
nn_file = os.path.join(OUT_DIR, "f_grad_norms.npy")
f_grad_norms = np.load(nn_file)

# Plot Lipschitz gradient norms to check if unit norm is approximately satisfied (as enforced by the penalty term)
plot_lipschitz(inner_steps=inner_steps,
               outer_steps=mapper_num_iters,
               samples=f_grad_norms,
               type='penalty')
plt.savefig(FIG_DIR + '/Lipschitz_gradient_norm.png', bbox_inches='tight')

# Obtain concatenated parameter gradient norms
param_file = os.path.join(OUT_DIR, "p_grad_norms.npy")
p_grad_norms = np.load(param_file)

# Plot norms of concatenated parameter gradients dL/dp for loss L, weights/biases p, to measure "network change"
plot_lipschitz(inner_steps=inner_steps,
               outer_steps=mapper_num_iters,
               samples=p_grad_norms,
               type='parameter')
plt.savefig(FIG_DIR + '/parameter_gradients_norm.png', bbox_inches='tight')

# Obtain Lipschitz losses
loss_file = os.path.join(OUT_DIR, "lip_losses.npy")
lip_losses = np.load(loss_file)

# Plot Lipschitz losses to assess convergence towards Wasserstein distance (the maximum)
plot_lipschitz(inner_steps=inner_steps,
               outer_steps=mapper_num_iters,
               samples=lip_losses,
               type='loss')
plt.savefig(FIG_DIR + '/Lipschitz_losses.png', bbox_inches='tight')

num_ckpt = mapper_num_iters // 50  # number of 50-step checkpoints with saved data

# Plot the sampled predictions for desired checkpoints
plt_checkpoints = np.unique([10/50, 1, 2] + list(range(4, num_ckpt, 4)) + [num_ckpt])
plt_checkpoints.sort()  # ascending order by default
opt_bnn_mean0 = None
opt_bnn_sd0 = None

for ckpt in plt_checkpoints:  # at [1], 10, 50, 200, 400, ..., and mapper_num_iters steps

    # Load optimised BNN from saved checkpoint (dictionary with parameter values)
    ckpt_path = os.path.join(OUT_DIR, "ckpts", "it-{}.ckpt".format(int(50*ckpt)))
    opt_bnn.load_state_dict(torch.load(ckpt_path))

    # Perform predictions using n_plot sets of sampled weights/biases - rows inputs, cols samples
    # opt_bnn_samples = opt_bnn.sample_functions(test_tensor, n_plot).detach().cpu().numpy().squeeze()
    opt_bnn_samples = np.array([opt_bnn(test_tensor).detach().cpu().numpy() for _ in range(n_plot)]).squeeze().T

    # Resize array of predictions
    opt_bnn_samples_ = np.zeros((n_plot, n_test, n_test))
    for ss in range(n_plot):
        sample = opt_bnn_samples[:, ss].T
        sample_ = sample.reshape(n_test, -1)
        opt_bnn_samples_[ss, :, :] = sample_

    if (len(plt_checkpoints) > 1) & (ckpt == plt_checkpoints[0]):
        # Obtain mean and std dev of samples
        opt_bnn_mean0 = np.mean(opt_bnn_samples_, axis=0).squeeze()
        opt_bnn_sd0 = np.std(opt_bnn_samples_, axis=0).squeeze()

    # Obtain mean and std dev of samples
    opt_bnn_mean = np.mean(opt_bnn_samples_, axis=0).squeeze()
    opt_bnn_sd = np.std(opt_bnn_samples_, axis=0).squeeze()

    # Plot the optimised BNN at this checkpoint
    plot_mean_sd(mean_grid=opt_bnn_mean,
                 sd_grid=opt_bnn_sd,
                 domain=test_tensor)
    plt.savefig(FIG_DIR + '/GPiG_prior_step{}.png'.format(int(50*ckpt)), bbox_inches='tight')

    # Plot 4-by-4 panels of samples
    plot_samples_2d(samples=opt_bnn_samples_,
                    extent=[0, 1],
                    figsize=(13.75,12))
    plt.savefig(FIG_DIR + '/GPiG_prior_samples_step{}.png'.format(int(50*ckpt)), bbox_inches='tight')

# Compute covariance matrices between all inputs
sst_big_cov_mx = np.cov(sst_samples)  # empirical SST covariances
bnn_big_cov_mx = np.cov(opt_bnn_samples) # final step BNN covariances

# Nonstationary covariance heatmaps from SST data samples
cov_min, cov_max = plot_cov_nonstat(cov=sst_big_cov_mx,
                                    domain=test_tensor)
plt.savefig(FIG_DIR + '/SST_cov_heatmaps.png', bbox_inches='tight')

# Do the same for GPi-G covariances (using same points)
plot_cov_nonstat(cov=bnn_big_cov_mx,
                 domain=test_tensor,
                 cov_min=cov_min,
                 cov_max=cov_max)
plt.savefig(FIG_DIR + '/BNN_nonstat_cov_heatmaps.png', bbox_inches='tight')

# Obtain min/max for SD and mean, for consistent value ranges in plots
sd_min_list = [np.min(sst_sd), np.min(std_bnn_sd), np.min(opt_bnn_sd)]
sd_max_list = [np.max(sst_sd), np.max(std_bnn_sd), np.max(opt_bnn_sd)]
mean_min_list = [np.min(sst_mean), np.min(std_bnn_mean), np.min(opt_bnn_mean)]
mean_max_list = [np.max(sst_mean), np.max(std_bnn_mean), np.max(opt_bnn_mean)]
if opt_bnn_sd0 is not None:
    sd_min_list.append(np.min(opt_bnn_sd0))
    sd_max_list.append(np.max(opt_bnn_sd0))
if opt_bnn_mean0 is not None:
    mean_min_list.append(np.min(opt_bnn_mean0))
    mean_max_list.append(np.max(opt_bnn_mean0))
sd_min0, sd_max0 = min(sd_min_list), max(sd_max_list)
mean_min0, mean_max0 = min(mean_min_list), max(mean_max_list)

# Plot the GP prior using sampled functions
plot_mean_sd(mean_grid=sst_mean,
             sd_grid=sst_sd,
             domain=test_tensor,
             sd_range=[sd_min0, sd_max0],
             mean_range=[mean_min0, mean_max0])
plt.savefig(FIG_DIR + '/SST_prior.png', bbox_inches='tight')

# Plot the sampled BNN predictions
plot_mean_sd(mean_grid=std_bnn_mean,
             sd_grid=std_bnn_sd,
             domain=test_tensor,
             sd_range=[sd_min0, sd_max0],
             mean_range=[mean_min0, mean_max0])
plt.savefig(FIG_DIR + '/std_prior.png', bbox_inches='tight')

if (opt_bnn_sd0 is not None) & (opt_bnn_mean0 is not None):
    # Plot an early GPiG prior using sampled functions
    plot_mean_sd(mean_grid=opt_bnn_mean0,
                 sd_grid=opt_bnn_sd0,
                 domain=test_tensor,
                 sd_range=[sd_min0, sd_max0],
                 mean_range=[mean_min0, mean_max0])
    plt.savefig(FIG_DIR + '/GPiG_prior_step{}.png'.format(int(plt_checkpoints[0]*50)), bbox_inches='tight')

# Plot the final GPiG prior using sampled functions
plot_mean_sd(mean_grid=opt_bnn_mean,
             sd_grid=opt_bnn_sd,
             domain=test_tensor,
             sd_range=[sd_min0, sd_max0],
             mean_range=[mean_min0, mean_max0])
plt.savefig(FIG_DIR + '/GPiG_prior.png', bbox_inches='tight')

"""
Generate Observations
"""

# Each approx 16x16 subregion (dividing evenly into 4x4 squares) has 256 inputs
# Training partition (periphery) thus has approx 12 * 256 inputs
# Holdout partition (deleted centre) has approx 4 * 256 inputs

# Specify input points for training and holdout data sets
test_range1 = test_range[:n_test//4]  # lower quarter of x1/x2 indices
test_range2 = test_range[n_test//4:n_test//2]  # second quarter of x1/x2 indices
test_range3 = test_range[n_test//2:3*n_test//4]  # third quarter of x1/x2 indices
test_range4 = test_range[3*n_test//4:]  # upper quarter of x1/x2 indices
train_range = np.hstack([test_range1, test_range4])  # join vectors into a longer vector
holdout_range = np.hstack([test_range2, test_range3])  # ditto
train_partition = test_array[(np.in1d(test_array[:, 0], train_range)) | (np.in1d(test_array[:, 1], train_range))]
holdout_partition = test_array[(np.in1d(test_array[:, 0], holdout_range)) & (np.in1d(test_array[:, 1], holdout_range))]
n_edge = train_partition.shape[0]  # number of points in periphery
n_inside = holdout_partition.shape[0]  # number of points in deleted centre
n_train = 100 * 12  # size of training data set
train_subset = np.random.choice(np.arange(train_partition.shape[0]), size=n_train, replace=False)
train_subset.sort()
train_array = train_partition[train_subset, :]  # training data points

# Obtain input-target pairs for training and test data sets
train_inds_ = [int(np.argwhere(np.all(test_array == train_array[rr, :], axis=1)).item()) for rr in range(n_train)]
edge_inds_ = [int(np.argwhere(np.all(test_array == train_partition[rr, :], axis=1)).item()) for rr in range(n_edge)]
inside_inds_ = [int(np.argwhere(np.all(test_array == holdout_partition[rr, :], axis=1)).item()) for rr in range(n_inside)]
train_inds = np.unique(train_inds_).tolist()
edge_inds = np.unique(edge_inds_).tolist()
inside_inds = np.unique(inside_inds_).tolist()
test_edge_inds = list(set(edge_inds) - set(train_inds))
test_edge_inds.sort()
#test_edge_inds = np.setdiff1d(edge_inds, train_inds)
test_inds = np.sort(np.hstack([inside_inds, test_edge_inds]))
n_holdout = len(test_inds)  # total number of unobserved test points

X = test_array[train_inds, :]
y = sst_latent[train_inds]
X_edge = test_array[test_edge_inds, :]
y_edge = sst_latent[test_edge_inds]
X_inside = test_array[inside_inds, :]
y_inside = sst_latent[inside_inds]
X_tensor = test_tensor[train_inds, :]

# Plot latent function (to compare with sample mean plots)
plt.figure()
plt.contourf(X1, X2, sst_latent_, levels=256, cmap='Spectral_r', origin='lower')
plt.axis('scaled')
plt.colorbar(pad=0.01)
plt.savefig(FIG_DIR + '/latent_function.png', bbox_inches='tight')

# Plot observed points of latent function
plt.figure()
sst_min = torch.min(sst_latent)
sst_max = torch.max(sst_latent)
plt.scatter(X[:, 0], X[:, 1], c=y, cmap='Spectral_r', vmin=sst_min, vmax=sst_max)
plt.axis('scaled')
ax = plt.gca()
ax.set_facecolor('whitesmoke')
plt.colorbar(pad=0.01)
plt.savefig(FIG_DIR + '/latent_observations.png', bbox_inches='tight')

"""
GP prior
"""

# Initial values for GP kernel hyperparameters (use flexible settings, i.e. high ampl and low leng)
sn2 = 0.001   # measurement error variance
leng = 0.1  # lengthscale
ampl = 10.  # amplitude

# Specify GP kernel
kernel_class = base.Isotropic
kernel_fctn = kernels.Matern32
kernel = kernel_class(cov=kernel_fctn,
                      ampl=ampl,
                      leng=leng)

# Initialize GP prior
gp = GP(kern=kernel).to(device)

# Sample functions from GP prior
gp_samples = gp.sample_functions(test_tensor, n_plot + 1).detach().cpu().numpy().squeeze()

# Resize array of predictions
gp_samples_ = np.zeros((n_plot, 64, 64))
for ss in range(n_plot):
    sample = gp_samples[:, ss].T
    sample_ = sample.reshape(64, -1)
    gp_samples_[ss, :, :] = sample_

# Obtain mean and std dev of samples
gp_mean = np.mean(gp_samples_, axis=0)
gp_sd = np.std(gp_samples_, axis=0)

# Plot the GP prior using sampled functions
plot_mean_sd(mean_grid=gp_mean,
             sd_grid=gp_sd,
             domain=test_tensor)
plt.savefig(FIG_DIR + '/GP_prior.png', bbox_inches='tight')

# Plot 4-by-4 panels of samples for GP prior
plot_samples_2d(samples=gp_samples_,
                extent=[0, 1],
                figsize=(13.75,12))
plt.savefig(FIG_DIR + '/GP_prior_samples.png', bbox_inches='tight')

"""
GP Posterior and Maximum Likelihood Estimation
"""

# Condition on data to obtain GP posterior
GP.assign_data(gp,
               X=X_tensor,
               Y=y.reshape(-1, 1),
               sn2=sn2)

# Function to use as objective in MLE (optimise in log space)
def objective(log_hyper):
    ampl, leng, sn2 = [np.exp(x) for x in log_hyper]
    loglik, *_ = gp.marginal_loglik(ampl=ampl, leng=leng, sn2=sn2)
    return -loglik

# Optimisation of ML (in log space) to obtain kernel hyperparameter estimates
hyper_init = np.asarray((ampl, leng, sn2))
hyper_init_log = np.log(hyper_init)

# Optimisation of ML to obtain kernel hyperparameter estimates
ML_optim = basinhopping(func=objective,
                        x0=hyper_init_log,
                        niter=100,
                        minimizer_kwargs={'method': 'BFGS'})

# Extract optimised hyperparameters, and terms in likelihood
hyper_opt_log = ML_optim.get('x')
hyper_opt = [np.exp(x) for x in hyper_opt_log]
opt_ampl, opt_leng, opt_sn2 = hyper_opt
loglik0, modelfit0, complexity0 = gp.marginal_loglik(ampl=ampl, leng=leng, sn2=sn2)
loglik, modelfit, complexity = gp.marginal_loglik(ampl=opt_ampl, leng=opt_leng, sn2=opt_sn2)
print('Initial GP: amplitude {}, length-scale {}, measurement error variance {}'.format(ampl, leng, sn2))
print('Optimised GP: amplitude {}, length-scale {}, measurement error variance {}'.format(opt_ampl, opt_leng, opt_sn2))

# Perform predictions with initial GP posterior
gp_preds_init = gp.predict_f_samples(test_tensor,
                                     n_samples=n_plot,
                                     noisy_targets=True).detach().cpu().numpy().squeeze()

# Update GP with optimised hyperparameters
GP.update_kernel(gp, ampl=opt_ampl, leng=opt_leng, sn2=opt_sn2)

# Perform predictions with optimised GP posterior
gp_preds_opt = gp.predict_f_samples(test_tensor,
                                    n_samples=n_plot,
                                    noisy_targets=True).detach().cpu().numpy().squeeze()

# Resize array of predictions
gp_preds_init_ = np.zeros((n_plot, 64, 64))
gp_preds_opt_ = np.zeros((n_plot, 64, 64))
for ss in range(n_plot):
    sample = gp_preds_init[:, ss].T
    sample_ = sample.reshape(64, -1)
    gp_preds_init_[ss, :, :] = sample_
    sample = gp_preds_opt[:, ss].T
    sample_ = sample.reshape(64, -1)
    gp_preds_opt_[ss, :, :] = sample_

# Obtain mean and std dev of samples
gp_init_mean = np.mean(gp_preds_init_, axis=0)
gp_init_sd = np.std(gp_preds_init_, axis=0)
gp_opt_mean = np.mean(gp_preds_opt_, axis=0)
gp_opt_sd = np.std(gp_preds_opt_, axis=0)
gp_var_test = np.var(gp_preds_opt, axis=1)  # test set predictive variance (optimal GP only)
gp_var_test_inside = gp_var_test[inside_inds]  # long-range predictive variance
gp_var_test_edge = gp_var_test[test_edge_inds]  # short-range predictive variance
print('GP posterior long-range predictive variance (shape {}): mean {}'
      .format(gp_var_test_inside.shape, gp_var_test_inside.mean()))
print('GP posterior short-range predictive variance (shape {}): mean {}'
      .format(gp_var_test_edge.shape, gp_var_test_edge.mean()))

# Plot 4-by-4 panels of samples for initial GP posterior
plot_samples_2d(samples=gp_preds_init_,
                extent=[0, 1],
                figsize=(13.75,12))
plt.savefig(FIG_DIR + '/GP_posterior_init_samples.png', bbox_inches='tight')

# Plot 4-by-4 panels of samples for optimised GP posterior
plot_samples_2d(samples=gp_preds_opt_,
                extent=[0, 1],
                figsize=(13.75,12))
plt.savefig(FIG_DIR + '/GP_posterior_opt_samples.png', bbox_inches='tight')

# Prediction performance metrics for long-range interpolation
coverage_perc = 90
score_alpha = 0.1
gp_rmspe_inside = rmspe(preds=gp_preds_opt[inside_inds, :].T,
                        obs=y_inside,
                        return_all=False)
gp_coverage_inside = perc_coverage(preds=gp_preds_opt[inside_inds, :].T,
                                   obs=y_inside,
                                   me_var=opt_sn2,
                                   percent=coverage_perc)
gp_score_inside = interval_score(preds=gp_preds_opt[inside_inds, :].T,
                                 obs=y_inside,
                                 me_var=opt_sn2,
                                 alpha=score_alpha)
print('Prediction performance metrics for GP posterior (long-range interpolation)')
print('RMSPE: {}\n{}-Percent Coverage: {}\nInterval Score (alpha = {}): {}'
      .format(gp_rmspe_inside, coverage_perc, gp_coverage_inside, score_alpha, gp_score_inside))

# Prediction performance metrics for short-range interpolation
gp_rmspe_edge = rmspe(preds=gp_preds_opt[test_edge_inds, :].T,
                      obs=y_edge,
                      return_all=False)
gp_coverage_edge = perc_coverage(preds=gp_preds_opt[test_edge_inds, :].T,
                                 obs=y_edge,
                                 me_var=opt_sn2,
                                 percent=coverage_perc)
gp_score_edge = interval_score(preds=gp_preds_opt[test_edge_inds, :].T,
                               obs=y_edge,
                               me_var=opt_sn2,
                               alpha=score_alpha)
print('Prediction performance metrics for GP posterior (short-range interpolation)')
print('RMSPE: {}\n{}-Percent Coverage: {}\nInterval Score (alpha = {}): {}'
      .format(gp_rmspe_edge, coverage_perc, gp_coverage_edge, score_alpha, gp_score_edge))

to_save = {'sn2': sn2,
           'leng': leng,
           'ampl': ampl,
           'opt_sn2': opt_sn2,
           'opt_leng': opt_leng,
           'opt_ampl': opt_ampl,
           'gp_var_test_inside': gp_var_test_inside.mean(),
           'gp_var_test_edge': gp_var_test_edge.mean(),
           'kernel_class': kernel_class,
           'kernel_fctn': kernel_fctn,
           'loglik': loglik,
           'modelfit': modelfit,
           'complexity': complexity,
           'loglik0': loglik0,
           'modelfit0': modelfit0,
           'complexity0': complexity0,
           'gp_rmspe_inside': gp_rmspe_inside,
           'gp_coverage_inside': gp_coverage_inside,
           'gp_score_inside': gp_score_inside,
           'gp_rmspe_edge': gp_rmspe_edge,
           'gp_coverage_edge': gp_coverage_edge,
           'gp_score_edge': gp_score_edge}
pickle.dump(to_save, stage1_file)

# Save all parameter settings for stage 1 code
stage1_file.close()
stage1_file = open(OUT_DIR + "/stage1.file", "rb")
while True:
    try:
        stage1_txt.write(str(pickle.load(stage1_file)) + '\n')
    except EOFError:
        break

"""
SGHMC - Fixed BNN posterior
"""

# SGHMC Hyper-parameters (see sampling_configs comments for interpretation)
n_chains = 4
keep_every = 500
n_samples = 200
n_burn = 3000  # must be multiple of keep_every
n_burn_thin = n_burn // keep_every
n_discarded = 100 - n_burn_thin
sampler = 'adaptive_sghmc'
sghmc_lr = 1e-4
adaptive_lr = sghmc_lr ** 0.5
if sampler == 'sghmc':
    sampler_lr = sghmc_lr
elif sampler == 'adaptive_sghmc':
    sampler_lr = adaptive_lr
else:
    raise Exception('Only `sghmc` and `adaptive_sghmc` samplers are supported.')

sampling_configs = {
    "batch_size": 32,  # Mini-batch size
    "num_samples": n_samples,  # Number of samples kept for each chain (default 30)
    "n_discarded": n_discarded,  # Number of samples to discard after burn-in adaptation phase (default 10)
    "num_burn_in_steps": n_burn,  # Number of burn-in steps for adaptive SGHMC (default 2000)
    "keep_every": keep_every,  # Thinning interval (default 200)
    "lr": sampler_lr,  # Step size (1e-4 works well for standard SGHMC, and 1e-2 for adaptive)
    "mdecay": 0.05,  # Momentum coefficient
    "num_chains": n_chains,  # Number of Markov chains (for r_hat, at least 4 suggested)
    "print_every_n_samples": 50,  # Control feedback regularity
}

pickle.dump({'n_train': n_train, 'sampler': sampler, 'sampler_lr': sampler_lr}, stage2_file)
pickle.dump(sampling_configs, stage2_file)

# Initialize the prior and likelihood
prior = FixedGaussianPrior(mu=0, std=1)  # same settings as fixed BNN prior
likelihood = LikGaussian(opt_sn2)

# Set up neural network for usage with SGHMC
net = BlankNet(output_dim=1,
               hidden_dims=hidden_dims,
               activation_fn=transfer_fn).to(device)

# Compare named parameters of the neural networks
print('GaussianNet has named parameters:\n{}'.format([name for name, param in opt_bnn.named_parameters()]))
print('BlankNet has named parameters:\n{}'.format([name for name, param in net.named_parameters()]))

# Generate posterior BNN parameter samples with multiple chains of sampling
bayes_net_std = BayesNet(net, likelihood, prior,
                         sampling_method=sampler,
                         n_gpu=0)
bayes_net_std.add_embedding_layer(input_dim=2,
                                  rbf_dim=embed_width,
                                  domain=test_tensor,
                                  rbf_ls=rbf_ls)
std_weights, std_weights_pred = bayes_net_std.sample_multi_chains(X, y, **sampling_configs)

n_samples_all_chains = len(std_weights)  # total number of samples from all chains, including discarded burn-in samples
n_samples_pred_chains = len(std_weights_pred)
n_samples_none = len(list(filter(lambda item: item is None, std_weights)))
n_samples_pred_none = len(list(filter(lambda item: item is None, std_weights_pred)))
print('Sample quantities')
print('Total samples including discarded: {}\n'
      'None values in total samples: {}\n'
      'Samples used for prediction: {}\n'
      'None values in samples for prediction : {}\n'
      'Samples per chain * Number of chains : {}'
      .format(n_samples_all_chains, n_samples_none, n_samples_pred_chains, n_samples_pred_none, n_samples * n_chains))

# Create arrays of MCMC samples (samples cols, parameters rows)
std_chains = np.zeros((2 * depth, n_samples_all_chains))
std_chains_pred = np.zeros((2 * depth, n_chains * n_samples))

# Populate the array containing all samples
for k in range(n_samples_all_chains):
    std_dict = std_weights[k]  # dictionary of name-tensor pairs for network parameters
    W_tensors = list(std_dict.values())[0::2]
    b_tensors = list(std_dict.values())[1::2]
    for ll in range(2 * depth):
        if ll % 2 == 0:
            W_idx = tuple([0] * W_tensors[ll // 2].dim())
            std_chains[ll, k] = W_tensors[ll // 2][W_idx]  # use first weight entry
        else:
            b_idx = tuple([0] * b_tensors[(ll - 1) // 2].dim())
            std_chains[ll, k] = b_tensors[(ll - 1) // 2][b_idx]  # use first bias entry

# Populate the array only containing samples utilised for predictions
for k in range(n_samples * n_chains):
    std_dict = std_weights_pred[k]  # dictionary of name-tensor pairs for network parameters
    W_tensors = list(std_dict.values())[0::2]
    b_tensors = list(std_dict.values())[1::2]
    for ll in range(2 * depth):
        if ll % 2 == 0:
            W_idx = tuple([0] * W_tensors[ll // 2].dim())
            std_chains_pred[ll, k] = W_tensors[ll // 2][W_idx]  # use first weight entry
        else:
            b_idx = tuple([0] * b_tensors[(ll - 1) // 2].dim())
            std_chains_pred[ll, k] = b_tensors[(ll - 1) // 2][b_idx]  # use first bias entry

# Construct vector with subplot titles (accounts for varying network depth)
trace_titles = []
for lvl in range(depth):
    W_l = r'$\mathbf{{W}}_{}$'.format(lvl + 1)
    b_l = r'$\mathbf{{b}}_{}$'.format(lvl + 1)
    trace_titles.append(W_l)
    trace_titles.append(b_l)

# Construct vector of legend entries (accounts for varying chain length)
legend_entries = ['Chain {}'.format(cc+1) for cc in range(n_chains)]

# Trace plots of MCMC iterates
print('Plotting parameter traces (fixed BNN)')
plot_param_traces(param_chains=std_chains,
                  n_chains=n_chains,
                  net_depth=depth,
                  n_discarded=n_discarded,
                  n_burn=n_burn_thin,
                  trace_titles=trace_titles,
                  legend_entries=legend_entries)
plt.savefig(FIG_DIR + '/std_chains.png', bbox_inches='tight')

# Make predictions
bnn_std_preds, bnn_std_preds_all = bayes_net_std.predict(test_tensor)

# MCMC convergence diagnostics
r_hat = compute_rhat(bnn_std_preds, sampling_configs["num_chains"])
rhat_mean_fixed = float(r_hat.mean())
rhat_sd_fixed = float(r_hat.std())
print(r"R-hat: mean {:.4f} std {:.4f}".format(rhat_mean_fixed, rhat_sd_fixed))

to_save = {'rhat_mean_fixed': rhat_mean_fixed,
           'rhat_sd_fixed': rhat_sd_fixed}
pickle.dump(to_save, stage2_file)

# Resize array of predictions
bnn_std_preds_ = np.zeros((n_chains * n_samples, 64, 64))
for ss in range(n_chains * n_samples):
    sample = bnn_std_preds[ss, :]
    sample_ = sample.reshape(64, -1)
    bnn_std_preds_[ss, :, :] = sample_

# Obtain mean and std dev of samples
std_pred_mean = np.mean(bnn_std_preds_, axis=0)
std_pred_sd = np.std(bnn_std_preds_, axis=0)
std_var_test = np.var(bnn_std_preds, axis=0)  # test set predictive variance
std_var_test_inside = std_var_test[inside_inds]  # long-range predictive variance
std_var_test_edge = std_var_test[test_edge_inds]  # short-range predictive variance
print('Fixed BNN long-range predictive variance (shape {}): mean {}'
      .format(std_var_test_inside.shape, std_var_test_inside.mean()))
print('Fixed BNN short-range predictive variance (shape {}): mean {}'
      .format(std_var_test_edge.shape, std_var_test_edge.mean()))

# Plot 4-by-4 panels of samples
plot_samples_2d(samples=bnn_std_preds_,
                extent=[0, 1],
                figsize=(13.75,12))
plt.savefig(FIG_DIR + '/std_posterior_samples.png', bbox_inches='tight')

# Trace plot of network output at specified input point(s)
print('Plotting output traces (fixed BNN)')
plot_output_traces(domain=test_tensor,
                   preds=bnn_std_preds_all,
                   n_chains=n_chains,
                   n_discarded=n_discarded,
                   n_burn=n_burn_thin,
                   legend_entries=legend_entries)
plt.savefig(FIG_DIR + '/std_posterior_trace.png', bbox_inches='tight')

# Histogram of network output at same input point(s)
plot_output_hist(domain=test_tensor, preds=bnn_std_preds)
plt.savefig(FIG_DIR + '/std_posterior_hist.png', bbox_inches='tight')

# ACF of network output at same input point(s)
print('Plotting output ACF (fixed BNN)')
plot_output_acf(domain=test_tensor,
                preds=bnn_std_preds,
                n_chains=n_chains,
                n_samples_kept=n_samples)
plt.savefig(FIG_DIR + '/std_posterior_acf.png', bbox_inches='tight')

# Prediction performance metrics for long-range interpolation
std_rmspe_inside = rmspe(preds=bnn_std_preds[:, inside_inds],
                         obs=y_inside,
                         return_all=False)
std_coverage_inside = perc_coverage(preds=bnn_std_preds[:, inside_inds],
                                    obs=y_inside,
                                    me_var=opt_sn2,
                                    percent=coverage_perc)
std_score_inside = interval_score(preds=bnn_std_preds[:, inside_inds],
                                  obs=y_inside,
                                  me_var=opt_sn2,
                                  alpha=score_alpha)
print('Prediction performance metrics for fixed BNN (long-range interpolation)')
print('RMSPE: {}\n{}-Percent Coverage: {}\nInterval Score (alpha = {}): {}'
      .format(std_rmspe_inside, coverage_perc, std_coverage_inside, score_alpha, std_score_inside))

# Prediction performance metrics for short-range interpolation
std_rmspe_edge = rmspe(preds=bnn_std_preds[:, test_edge_inds],
                       obs=y_edge,
                       return_all=False)
std_coverage_edge = perc_coverage(preds=bnn_std_preds[:, test_edge_inds],
                                  obs=y_edge,
                                  me_var=opt_sn2,
                                  percent=coverage_perc)
std_score_edge = interval_score(preds=bnn_std_preds[:, test_edge_inds],
                                obs=y_edge,
                                me_var=opt_sn2,
                                alpha=score_alpha)
print('Prediction performance metrics for fixed BNN (short-range interpolation)')
print('RMSPE: {}\n{}-Percent Coverage: {}\nInterval Score (alpha = {}): {}'
      .format(std_rmspe_edge, coverage_perc, std_coverage_edge, score_alpha, std_score_edge))

to_save = {'coverage_perc': coverage_perc,
           'score_alpha': score_alpha,
           'std_rmspe_inside': std_rmspe_inside,
           'std_coverage_inside': std_coverage_inside,
           'std_score_inside': std_score_inside,
           'std_rmspe_edge': std_rmspe_edge,
           'std_coverage_edge': std_coverage_edge,
           'std_score_edge': std_score_edge,
           'std_var_test_inside': std_var_test_inside,
           'std_var_test_edge': std_var_test_edge}
pickle.dump(to_save, stage2_file)

"""
SGHMC - GP-induced BNN posterior
"""

# Plot the estimated posteriors for desired checkpoints
mcmc_checkpoints = np.unique([10/50, 1, num_ckpt])
mcmc_checkpoints = [num_ckpt]
for ckpt in mcmc_checkpoints:  # at 1, 10, 50, 200, 400, ..., and mapper_num_iters steps

    # Load the optimized prior
    ckpt_path = os.path.join(OUT_DIR, "ckpts", "it-{}.ckpt".format(int(50*ckpt)))
    prior = OptimGaussianPrior(saved_path=ckpt_path, rbf=rbf)  # use optimised prior on each parameter

    # Obtain network parameter values (for plotting only)
    #opt_bnn.load_state_dict(torch.load(ckpt_path))

    # Generate posterior BNN parameter samples with multiple chains of sampling
    bayes_net_optim = BayesNet(net, likelihood, prior,
                               sampling_method=sampler,
                               n_gpu=0)
    bayes_net_optim.add_embedding_layer(input_dim=2,
                                        rbf_dim=embed_width,
                                        domain=test_tensor,
                                        rbf_ls=rbf_ls)
    bayes_net_optim.make_nonstationary(grid_width=3,
                                       grid_height=3)
    bnn_idxs = bayes_net_optim.bnn_idxs  # order of bnn_idxs determines order of panels in plot_bnn_grid
    grid_size = bayes_net_optim.grid_size
    optim_weights, optim_weights_pred = bayes_net_optim.sample_multi_chains(X, y, **sampling_configs)

    # Create arrays of MCMC samples (samples cols, parameters rows)
    optim_chains = np.zeros((2 * depth, n_samples_all_chains, grid_size))
    optim_chains_pred = np.zeros((2 * depth, n_chains * n_samples, grid_size))

    # Populate the array containing all samples
    for gg in range(grid_size):
        for k in range(n_samples_all_chains):
            optim_dict = optim_weights[gg][k]  # network parameter dictionary (one location only)
            W_tensors = list(optim_dict.values())[0::2]
            b_tensors = list(optim_dict.values())[1::2]
            for ll in range(2 * depth):
                if ll % 2 == 0:
                    W_idx = tuple([0] * W_tensors[ll // 2].dim())
                    optim_chains[ll, k, gg] = W_tensors[ll // 2][W_idx]  # use first weight entry
                else:
                    b_idx = tuple([0] * b_tensors[(ll - 1) // 2].dim())
                    optim_chains[ll, k, gg] = b_tensors[(ll - 1) // 2][b_idx]  # use first bias entry

    # Populate the array only containing samples utilised for predictions
    for gg in range(grid_size):
        for k in range(n_chains * n_samples):
            optim_dict = optim_weights[gg][k]  # network parameter dictionary (one location only)
            W_tensors = list(optim_dict.values())[0::2]
            b_tensors = list(optim_dict.values())[1::2]
            for ll in range(2 * depth):
                if ll % 2 == 0:
                    W_idx = tuple([0] * W_tensors[ll // 2].dim())
                    optim_chains_pred[ll, k, gg] = W_tensors[ll // 2][W_idx]  # use first weight entry
                else:
                    b_idx = tuple([0] * b_tensors[(ll - 1) // 2].dim())
                    optim_chains_pred[ll, k, gg] = b_tensors[(ll - 1) // 2][b_idx]  # use first bias entry

    # Make predictions
    bnn_optim_preds, bnn_optim_preds_all, bnn_optim_grid, bnn_optim_grid_all \
        = bayes_net_optim.predict(test_tensor)  # rows samples, cols traces
    opt_var_test = np.var(bnn_std_preds, axis=0)  # test set predictive variance
    opt_var_test_inside = opt_var_test[inside_inds]  # long-range predictive variance
    opt_var_test_edge = opt_var_test[test_edge_inds]  # short-range predictive variance
    print('GPi-G BNN long-range predictive variance (shape {}): mean {}'
          .format(opt_var_test_inside.shape, opt_var_test_inside.mean()))
    print('GPi-G BNN short-range predictive variance (shape {}): mean {}'
          .format(opt_var_test_edge.shape, opt_var_test_edge.mean()))

    # Check BNN locations
    print('BNN locations in test_tensor[bnn_idxs]\n{}'.format(test_tensor[bnn_idxs]))

    grid_size = bnn_optim_grid.shape[0]
    grid_len = int(np.sqrt(grid_size))

    # Check tensor values
    equal12 = np.sum(bnn_optim_grid[0, :, :] == bnn_optim_grid[1, :, :])
    equal13 = np.sum(bnn_optim_grid[0, :, :] == bnn_optim_grid[2, :, :])
    equal14 = np.sum(bnn_optim_grid[0, :, :] == bnn_optim_grid[3, :, :])
    print('equal12 {}, equal13 {}, equal14 {}'.format(equal12,equal13,equal14))

    # MCMC convergence diagnostics
    print('Computing R-hat for each optimised BNN posterior')
    r_hat_bnn = [compute_rhat(bnn_optim_grid[gg, :, :], sampling_configs["num_chains"])
                 for gg in range(bnn_optim_grid.shape[0])]
    print('Computing R-hat for collated chain of predictions')
    r_hat = compute_rhat(bnn_optim_preds, sampling_configs["num_chains"])
    print('Computing mean and std deviation of R-hat values')
    rhat_mean_each = [float(rhat.mean()) for rhat in r_hat_bnn]
    rhat_sd_each = [float(rhat.std()) for rhat in r_hat_bnn]
    rhat_mean_all = float(np.asarray(r_hat_bnn).mean())
    rhat_sd_all = float(np.asarray(r_hat_bnn).std())
    rhat_mean_opt = float(r_hat.mean())
    rhat_sd_opt = float(r_hat.std())
    print(r"Each BNN R-hat: mean {} std {}".format(rhat_mean_each, rhat_sd_each))
    print(r"Average BNN R-hat: mean {:.4f} std {:.4f}".format(rhat_mean_all, rhat_sd_all))
    print(r"Collated R-hat: mean {:.4f} std {:.4f}".format(rhat_mean_opt, rhat_sd_opt))

    if ckpt == num_ckpt:
        to_save = {'bnn_locations': test_tensor[bnn_idxs],
                   'bnn_indices': bnn_idxs,
                   'rhat_mean_opt': rhat_mean_opt,
                   'rhat_sd_opt': rhat_sd_opt,
                   'rhat_mean_all': rhat_mean_all,
                   'rhat_sd_all': rhat_sd_all,
                   'rhat_mean_each': rhat_mean_each,
                   'rhat_sd_each': rhat_sd_each}
        pickle.dump(to_save, stage2_file)

    # Resize array of predictions
    bnn_optim_preds_ = np.zeros((n_chains * n_samples, 64, 64))
    bnn_optim_grid_ = np.zeros((grid_size, n_chains * n_samples, 64, 64))
    for ss in range(n_chains * n_samples):
        sample = bnn_optim_preds[ss, :]
        sample_ = sample.reshape(64, -1)
        bnn_optim_preds_[ss, :, :] = sample_
        for gg in range(grid_size):
            sample_g = bnn_optim_grid[gg, ss, :]
            sample_g_ = sample_g.reshape(64, -1)
            bnn_optim_grid_[gg, ss, :, :] = sample_g_

    # Obtain mean and std dev of samples
    opt_pred_mean = np.mean(bnn_optim_preds_, axis=0).squeeze()
    opt_pred_sd = np.std(bnn_optim_preds_, axis=0).squeeze()
    opt_grid_mean = np.mean(bnn_optim_grid_, axis=1).squeeze()
    opt_grid_sd = np.std(bnn_optim_grid_, axis=1).squeeze()

    if ckpt == num_ckpt:

        ##################
        # Diagnostic plots

        # Trace plots of MCMC iterates
        for gg in range(grid_size):
            print('Plotting parameter traces (optimised BNN) for grid point # {}'.format(gg + 1))
            plot_param_traces(param_chains=optim_chains[:, :, gg],
                              n_chains=n_chains,
                              net_depth=depth,
                              n_discarded=n_discarded,
                              n_burn=n_burn_thin,
                              trace_titles=trace_titles,
                              legend_entries=legend_entries)
            plt.savefig(FIG_DIR + '/optim_chains_{}.png'
                        .format(tuple(np.round(test_array[bnn_idxs[gg], :], 2))), bbox_inches='tight')

            # Trace plot of network output at specified grid point
            print('Plotting output traces (optimised BNN) for grid point # {}'.format(gg + 1))
            plot_output_traces(domain=test_tensor,
                               preds=bnn_optim_grid_all[gg, :, :],
                               n_chains=n_chains,
                               n_discarded=n_discarded,
                               n_burn=n_burn_thin,
                               legend_entries=legend_entries)
            plt.savefig(FIG_DIR + '/GPiG_posterior_trace_{}.png'
                        .format(tuple(np.round(test_array[bnn_idxs[gg], :], 2))), bbox_inches='tight')

        # Trace plot of collated network output
        print('Plotting output traces (Bayesian model average)')
        plot_output_traces(domain=test_tensor,
                           preds=bnn_optim_preds_all,
                           n_chains=n_chains,
                           n_discarded=n_discarded,
                           n_burn=n_burn_thin,
                           legend_entries=legend_entries)
        plt.savefig(FIG_DIR + '/GPiG_posterior_trace.png', bbox_inches='tight')

        # Histogram of network output at same input point(s)
        plot_output_hist(domain=test_tensor, preds=bnn_optim_preds)
        plt.savefig(FIG_DIR + '/GPiG_posterior_hist.png', bbox_inches='tight')

        # ACF of network output at same input point(s)
        print('Plotting output ACF (optimised BNN)')
        plot_output_acf(domain=test_tensor,
                        preds=bnn_optim_preds,
                        n_chains=n_chains,
                        n_samples_kept=n_samples)
        plt.savefig(FIG_DIR + '/GPiG_posterior_acf.png', bbox_inches='tight')

    if (len(mcmc_checkpoints) > 1) & (ckpt < num_ckpt):
        # Plot the sampled predictions from the GP-induced BNN posterior (at each checkpoint)
        plot_mean_sd(mean_grid=opt_pred_mean,
                     sd_grid=opt_pred_sd,
                     domain=test_tensor)
        plt.savefig(FIG_DIR + '/GPiG_posterior_step{}.png'.format(int(50*ckpt)), bbox_inches='tight')

    # Plot 4-by-4 panels of samples
    plot_samples_2d(samples=bnn_optim_preds_,
                    extent=[0, 1],
                    figsize=(13.75,12))
    plt.savefig(FIG_DIR + '/GPiG_posterior_samples_step{}.png'.format(int(50 * ckpt)), bbox_inches='tight')

    # Plot final samples from each trained BNN
    plot_bnn_grid(bnn_grid=bnn_optim_grid_[:, -1, :, :],
                  domain=test_tensor,
                  type='samples',
                  bnn_idxs=bnn_idxs,
                  figsize=(15,14))
    plt.savefig(FIG_DIR + '/GPiG_grid_samples_step{}.png'.format(int(50 * ckpt)), bbox_inches='tight')

    # Plot mean of each trained BNN
    plot_bnn_grid(bnn_grid=opt_grid_mean,
                  domain=test_tensor,
                  type='mean',
                  bnn_idxs=bnn_idxs,
                  figsize=(15,14))
    plt.savefig(FIG_DIR + '/GPiG_grid_mean_step{}.png'.format(int(50 * ckpt)), bbox_inches='tight')

    # Plot std dev of each trained BNN
    plot_bnn_grid(bnn_grid=opt_grid_sd,
                  domain=test_tensor,
                  type='sd',
                  bnn_idxs=bnn_idxs,
                  figsize=(15,14))
    plt.savefig(FIG_DIR + '/GPiG_grid_sd_step{}.png'.format(int(50 * ckpt)), bbox_inches='tight')

# Obtain min/max for SD and mean, for consistent value ranges in plots
sd_min_list = [np.min(std_pred_sd), np.min(opt_pred_sd), np.min(gp_opt_sd)]
sd_max_list = [np.max(std_pred_sd), np.max(opt_pred_sd), np.max(gp_opt_sd)]
mean_min_list = [np.min(std_pred_mean), np.min(opt_pred_mean), np.min(gp_opt_mean)]
mean_max_list = [np.max(std_pred_mean), np.max(opt_pred_mean), np.max(gp_opt_mean)]
sd_min, sd_max = min(sd_min_list), max(sd_max_list)
mean_min, mean_max = min(mean_min_list), max(mean_max_list)

# Plot the sampled predictions from the GP posterior
plot_mean_sd(mean_grid=gp_opt_mean,
             sd_grid=gp_opt_sd,
             domain=test_tensor,
             sd_range=[sd_min, sd_max],
             mean_range=[mean_min, mean_max])
plt.savefig(FIG_DIR + '/GP_posterior.png', bbox_inches='tight')

# Plot the sampled predictions from the fixed BNN posterior
plot_mean_sd(mean_grid=std_pred_mean,
             sd_grid=std_pred_sd,
             domain=test_tensor,
             sd_range=[sd_min, sd_max],
             mean_range=[mean_min, mean_max])
plt.savefig(FIG_DIR + '/std_posterior.png', bbox_inches='tight')

# Plot the sampled predictions from the GP-induced BNN posterior (at each checkpoint)
plot_mean_sd(mean_grid=opt_pred_mean,
             sd_grid=opt_pred_sd,
             domain=test_tensor,
             sd_range=[sd_min, sd_max],
             mean_range=[mean_min, mean_max])
plt.savefig(FIG_DIR + '/GPiG_posterior.png', bbox_inches='tight')

# Prediction performance metrics for long-range interpolation
opt_rmspe_inside = rmspe(preds=bnn_optim_preds[:, inside_inds],
                         obs=y_inside,
                         return_all=False)
opt_coverage_inside = perc_coverage(preds=bnn_optim_preds[:, inside_inds],
                                    obs=y_inside,
                                    me_var=opt_sn2,
                                    percent=coverage_perc)
opt_score_inside = interval_score(preds=bnn_optim_preds[:, inside_inds],
                                  obs=y_inside,
                                  me_var=opt_sn2,
                                  alpha=score_alpha)
print('Prediction performance metrics for GPi-G BNN (long-range interpolation)')
print('RMSPE: {}\n{}-Percent Coverage: {}\nInterval Score (alpha = {}): {}'
      .format(opt_rmspe_inside, coverage_perc, opt_coverage_inside, score_alpha, opt_score_inside))

# Prediction performance metrics for short-range interpolation
opt_rmspe_edge = rmspe(preds=bnn_optim_preds[:, test_edge_inds],
                       obs=y_edge,
                       return_all=False)
opt_coverage_edge = perc_coverage(preds=bnn_optim_preds[:, test_edge_inds],
                                  obs=y_edge,
                                  me_var=opt_sn2,
                                  percent=coverage_perc)
opt_score_edge = interval_score(preds=bnn_optim_preds[:, test_edge_inds],
                                obs=y_edge,
                                me_var=opt_sn2,
                                alpha=score_alpha)
print('Prediction performance metrics for GPi-G BNN (short-range interpolation)')
print('RMSPE: {}\n{}-Percent Coverage: {}\nInterval Score (alpha = {}): {}'
      .format(opt_rmspe_edge, coverage_perc, opt_coverage_edge, score_alpha, opt_score_edge))

to_save = {'opt_rmspe_inside': opt_rmspe_inside,
           'opt_coverage_inside': opt_coverage_inside,
           'opt_score_inside': opt_score_inside,
           'opt_rmspe_edge': opt_rmspe_edge,
           'opt_coverage_edge': opt_coverage_edge,
           'opt_score_edge': opt_score_edge,
           'opt_var_test_inside': opt_var_test_inside,
           'opt_var_test_edge': opt_var_test_edge}
pickle.dump(to_save, stage2_file)

# Save all parameter settings for stage 1 code
stage2_file.close()
stage2_file = open(OUT_DIR + "/stage2.file", "rb")
while True:
    try:
        stage2_txt.write(str(pickle.load(stage2_file)) + '\n')
    except EOFError:
        break


