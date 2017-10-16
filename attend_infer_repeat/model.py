import functools
import numpy as np
import tensorflow as tf
from tensorflow.contrib.distributions import Normal
from tensorflow.contrib.distributions.python.ops.kullback_leibler import kl as _kl

from cell import AIRCell
from evaluation import gradient_summaries
from ops import Loss, make_moving_average
from prior import geometric_prior, NumStepsDistribution, tabular_kl




class AIRModel(object):
    """Generic AIR model"""

    def __init__(self, obs, nums, max_steps, glimpse_size,
                 n_appearance, transition, input_encoder, glimpse_encoder, glimpse_decoder, transform_estimator,
                 steps_predictor,
                 output_std=1., discrete_steps=True, output_multiplier=1.,
                 explore_eps=None, debug=False, **kwargs):
        """Creates the model.

        :param obs: tf.Tensor, images
        :param nums: tf.Tensor, number of objects in images
            Note: it is not used for inference or training; could be removed from here.
        :param max_steps: int, maximum number of steps to take (or objects in the image)
        :param glimpse_size: tuple of ints, size of the attention glimpse
        :param n_appearance: int, number of latent variables describing an object
        :param transition: see :class: AIRCell
        :param input_encoder: see :class: AIRCell
        :param glimpse_encoder: see :class: AIRCell
        :param glimpse_decoder: see :class: AIRCell
        :param transform_estimator: see :class: AIRCell
        :param steps_predictor: see :class: AIRCell
        :param output_std: float, std. dev. of the output Gaussian distribution
        :param discrete_steps: see :class: AIRCell
        :param output_multiplier: float, a factor that multiplies the reconstructed glimpses
        :param explore_eps: see :class: AIRCell
        :param debug: see :class: AIRCell
        :param **kwargs: all other parameters are passed to AIRCell
        """

        self.obs = obs
        self.nums = nums
        self.max_steps = max_steps
        self.glimpse_size = glimpse_size

        self.n_appearance = n_appearance

        self.output_std = output_std
        self.discrete_steps = discrete_steps
        self.explore_eps = explore_eps
        self.debug = debug

        with tf.variable_scope(self.__class__.__name__):
            self.output_multiplier = tf.Variable(output_multiplier, dtype=tf.float32, trainable=False, name='canvas_multiplier')

            shape = self.obs.get_shape().as_list()
            self.batch_size = shape[0]
            self.img_size = shape[1:]
            self._build(transition, input_encoder, glimpse_encoder, glimpse_decoder, transform_estimator,
                        steps_predictor, kwargs)

    def _build(self, transition, input_encoder, glimpse_encoder, glimpse_decoder, transform_estimator,
               steps_predictor, kwargs):
        """Build the model. See __init__ for argument description"""

        if self.explore_eps is not None:
            self.explore_eps = tf.get_variable('explore_eps', initializer=self.explore_eps, trainable=False)

        self.cell = AIRCell(self.img_size, self.glimpse_size, self.n_appearance, transition,
                            input_encoder, glimpse_encoder, glimpse_decoder, transform_estimator, steps_predictor,
                            canvas_init=None,
                            discrete_steps=self.discrete_steps,
                            explore_eps=self.explore_eps,
                            debug=self.debug,
                            **kwargs)

        initial_state = self.cell.initial_state(self.obs)

        dummy_sequence = tf.zeros((self.max_steps, self.batch_size, 1), name='dummy_sequence')
        outputs, state = tf.nn.dynamic_rnn(self.cell, dummy_sequence, initial_state=initial_state, time_major=True)

        for name, output in zip(self.cell.output_names, outputs):
            setattr(self, name, output)

        self.final_state = state[-2]
        self.glimpse = tf.reshape(self.presence * tf.nn.sigmoid(self.glimpse),
                                  (self.max_steps, self.batch_size,) + tuple(self.glimpse_size))
        self.canvas = tf.reshape(self.canvas, (self.max_steps, self.batch_size,) + tuple(self.img_size))
        self.canvas *= self.output_multiplier

        self.final_canvas = self.canvas[-1]

        self.output_distrib = Normal(self.final_canvas, self.output_std)

        posterior_step_probs = tf.transpose(tf.squeeze(self.presence_prob))
        self.num_steps_distrib = NumStepsDistribution(posterior_step_probs)

        self.num_step_per_sample = tf.to_float(tf.squeeze(tf.reduce_sum(self.presence, 0)))
        self.num_step = tf.reduce_mean(self.num_step_per_sample)
        self.gt_num_steps = tf.squeeze(tf.reduce_sum(self.nums, 0))

    @staticmethod
    def _anneal_weight(init_val, final_val, anneal_type, global_step, anneal_steps, hold_for=0., steps_div=1.,
                       dtype=tf.float64):

        val, final, step, hold_for, anneal_steps, steps_div = (tf.cast(i, dtype) for i in
                                                               (init_val, final_val, global_step, hold_for, anneal_steps, steps_div))
        step = tf.maximum(step - hold_for, 0.)

        if anneal_type == 'exp':
            decay_rate = tf.pow(final / val, steps_div / anneal_steps)
            val = tf.train.exponential_decay(val, step, steps_div, decay_rate)

        elif anneal_type == 'linear':
            val = final + (val - final) * (1. - step / anneal_steps)
        else:
            raise NotImplementedError

        anneal_weight = tf.maximum(final, val)
        return anneal_weight

    def _prior_loss(self, what_prior, where_scale_prior, where_shift_prior,
                    num_steps_prior, global_step):
        """Creates KL-divergence term of the loss"""

        with tf.variable_scope('KL_divergence'):
            prior_loss = Loss()
            if num_steps_prior is not None:
                if num_steps_prior.anneal is not None:
                    with tf.variable_scope('num_steps_prior'):
                        nsp = num_steps_prior

                        hold_init = getattr(nsp, 'hold_init', 0.)
                        steps_div = getattr(nsp, 'steps_div', 1.)
                        steps_prior_success_prob = self._anneal_weight(nsp.init, nsp.final, nsp.anneal, global_step,
                                                                    nsp.steps, hold_init, steps_div)
                else:
                    steps_prior_success_prob = num_steps_prior.init
                self.steps_prior_success_prob = steps_prior_success_prob

                with tf.variable_scope('num_steps'):
                    prior = geometric_prior(steps_prior_success_prob, self.max_steps)
                    num_steps_posterior_prob = self.num_steps_distrib.prob()
                    steps_kl = tabular_kl(num_steps_posterior_prob, prior)
                    self.kl_num_steps_per_sample = tf.squeeze(tf.reduce_sum(steps_kl, 1))

                    self.kl_num_steps = tf.reduce_mean(self.kl_num_steps_per_sample)
                    tf.summary.scalar('kl_num_steps', self.kl_num_steps)

                    weight = getattr(num_steps_prior, 'weight', 1.)
                    prior_loss.add(self.kl_num_steps, self.kl_num_steps_per_sample, weight=weight)

            if num_steps_prior.analytic:
                # reverse cumsum of q(n) needed to compute \E_{q(n)} [ KL[ q(z|n) || p(z|n) ]]
                step_weight = num_steps_posterior_prob[..., 1:]
                step_weight = tf.transpose(step_weight, (1, 0))
                step_weight = tf.cumsum(step_weight, axis=0, reverse=True)
            else:
                step_weight = tf.squeeze(self.presence)

            self.prior_step_weight = step_weight


            # # this prevents optimising the expectation with respect to q(n)
            # # it's similar to the maximisation step of EM: we have a pre-computed expectation
            # # from the E step, and now we're maximising with respect to the argument of the expectation.
            # self.prior_step_weight = tf.stop_gradient(self.prior_step_weight)

            conditional_kl_weight = 1.
            if what_prior is not None:
                with tf.variable_scope('what'):

                    prior = Normal(what_prior.loc, what_prior.scale)
                    posterior = Normal(self.what_loc, self.what_scale)

                    what_kl = _kl(posterior, prior)
                    what_kl = tf.reduce_sum(what_kl, -1) * self.prior_step_weight
                    what_kl_per_sample = tf.reduce_sum(what_kl, 0)

                    self.kl_what = tf.reduce_mean(what_kl_per_sample)
                    tf.summary.scalar('kl_what', self.kl_what)
                    prior_loss.add(self.kl_what, what_kl_per_sample, weight=conditional_kl_weight)

            if where_scale_prior is not None and where_shift_prior is not None:
                with tf.variable_scope('where'):
                    usx, utx, usy, uty = tf.split(self.where_loc, 4, 2)
                    ssx, stx, ssy, sty = tf.split(self.where_scale, 4, 2)
                    us = tf.concat((usx, usy), -1)
                    ss = tf.concat((ssx, ssy), -1)

                    scale_distrib = Normal(us, ss)
                    scale_prior = Normal(where_scale_prior.loc, where_scale_prior.scale)
                    scale_kl = _kl(scale_distrib, scale_prior)

                    ut = tf.concat((utx, uty), -1)
                    st = tf.concat((stx, sty), -1)
                    shift_distrib = Normal(ut, st)

                    if 'loc' in where_shift_prior:
                        shift_mean = where_shift_prior.loc
                    else:
                        shift_mean = ut
                    shift_prior = Normal(shift_mean, where_shift_prior.scale)

                    shift_kl = _kl(shift_distrib, shift_prior)
                    where_kl = tf.reduce_sum(scale_kl + shift_kl, -1) * self.prior_step_weight
                    where_kl_per_sample = tf.reduce_sum(where_kl, 0)
                    self.kl_where = tf.reduce_mean(where_kl_per_sample)
                    tf.summary.scalar('kl_where', self.kl_where)
                    prior_loss.add(self.kl_where, where_kl_per_sample, weight=conditional_kl_weight)

        return prior_loss

    def _reinforce(self, importance_weight, decay_rate):
        """Implements REINFORCE for training the discrete probability distribution over number of steps and train-step
         for the baseline"""

        log_prob = self.num_steps_distrib.log_prob(self.num_step_per_sample)

        if self.baseline is not None:
            if not isinstance(self.baseline, tf.Tensor):
                self.baseline_module = self.baseline
                wt, we, p = (tf.transpose(i, (1, 0, 2)) for i in ((self.what, self.where, self.presence)))
                self.baseline = self.baseline_module(self.obs, wt, we, p, self.final_state)
                self.baseline_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                                       scope=self.baseline_module.variable_scope.name)
            importance_weight -= self.baseline

        if decay_rate is not None:
            axes = range(len(importance_weight.get_shape()))
            mean, var = tf.nn.moments(tf.squeeze(importance_weight), axes=axes)
            self.imp_weight_moving_mean = make_moving_average('imp_weight_moving_mean', mean, 0., decay_rate)
            self.imp_weight_moving_var = make_moving_average('imp_weight_moving_var', var, 1., decay_rate)

            factor = tf.maximum(tf.sqrt(self.imp_weight_moving_var), 1.)
            importance_weight = (importance_weight - self.imp_weight_moving_mean) / factor

        self.importance_weight = importance_weight
        axes = range(len(self.importance_weight.get_shape()))
        imp_weight_mean, imp_weight_var = tf.nn.moments(self.importance_weight, axes)
        tf.summary.scalar('imp_weight_mean', imp_weight_mean)
        tf.summary.scalar('imp_weight_var', imp_weight_var)

        reinforce_loss_per_sample = tf.stop_gradient(self.importance_weight) * log_prob
        self.reinforce_loss = tf.reduce_mean(reinforce_loss_per_sample)
        tf.summary.scalar('reinforce_loss', self.reinforce_loss)

        return self.reinforce_loss

    def _make_baseline_train_step(self, opt, loss, baseline, baseline_vars):
        baseline_target = tf.stop_gradient(loss)

        self.baseline_loss = .5 * tf.reduce_mean(tf.square(baseline_target - baseline))
        tf.summary.scalar('baseline_loss', self.baseline_loss)
        train_step = opt.minimize(self.baseline_loss, var_list=baseline_vars)
        return train_step

    def train_step(self, learning_rate, l2_weight=0., what_prior=None, where_scale_prior=None,
                   where_shift_prior=None,
                   num_steps_prior=None, use_prior=True,
                   use_reinforce=True, baseline=None, decay_rate=None,
                   optimizer=tf.train.RMSPropOptimizer, opt_kwargs=dict(momentum=.9, centered=True)):
        """Creates the train step and the global_step

        :param learning_rate: float or tf.Tensor
        :param l2_weight: float or tf.Tensor, if > 0. then adds l2 regularisation to the model
        :param what_prior: AttrDict or similar, with `loc` and `scale`, both floats
        :param where_scale_prior: AttrDict or similar, with `loc` and `scale`, both floats
        :param where_shift_prior: AttrDict or similar, with `loc` and `scale`, both floats
        :param num_steps_prior: AttrDict or similar, described as an example:

            >>> num_steps_prior = AttrDict(
            >>> anneal='exp',   # type of annealing of the prior; can be 'exp', 'linear' or None
            >>> init=1. - 1e-7, # initial value of the prior
            >>> final=1e-5,     # final value of the prior
            >>> steps_div=1e4,  # relevant for exponential annealing, see :func: tf.exponential_decay
            >>> steps=1e5,      # number of steps for annealing
            >>> analytic=True
            >>> )

        `init` and `final` describe success probability values in a geometric distribution; for example `init=.9` means
        that the probability of taking a single step is .9, two steps is .9**2 etc.

        :param use_prior: boolean, if False sets the KL-divergence loss term to 0
        :param use_reinforce: boolean, if False doesn't compute gradients for the number of steps
        :param baseline: callable or None, baseline for variance reduction of REINFORCE
        :param decay_rate: float, decay rate to use for exp-moving average for NVIL
        :return: train step and global step
        """

        num_steps_prior['analytic'] = getattr(num_steps_prior, 'analytic', True)

        self.l2_weight = l2_weight
        self.what_prior = what_prior
        self.where_scale_prior = where_scale_prior
        self.where_shift_prior = where_shift_prior
        self.num_steps_prior = num_steps_prior

        if not hasattr(self, 'baseline'):
            self.baseline = baseline

        self.use_prior = use_prior
        if self.use_prior is not None:
            self.use_prior = tf.Variable(self.use_prior, trainable=False, name='use_prior')
            self.toggle_prior = self.use_prior.assign(tf.logical_not(self.use_prior))

        self.use_reinforce = use_reinforce and self.discrete_steps

        with tf.variable_scope('loss'):
            global_step = tf.train.get_or_create_global_step()
            loss = Loss()
            self._train_step = []
            self.learning_rate = tf.Variable(learning_rate, name='learning_rate', trainable=False)
            make_opt = functools.partial(optimizer, **opt_kwargs)

            # Reconstruction Loss, - \E_q [ p(x | z, n) ]
            rec_loss_per_sample = -self.output_distrib.log_prob(self.obs)
            self.rec_loss_per_sample = tf.reduce_sum(rec_loss_per_sample, axis=(1, 2))
            self.rec_loss = tf.reduce_mean(self.rec_loss_per_sample)
            tf.summary.scalar('rec', self.rec_loss)
            loss.add(self.rec_loss, self.rec_loss_per_sample)

            # Prior Loss, KL[ q(z, n | x) || p(z, n) ]
            if use_prior is not None:
                self.prior_loss = self._prior_loss(what_prior, where_scale_prior,
                                                   where_shift_prior, num_steps_prior, global_step)
                tf.summary.scalar('prior', self.prior_loss.value)
                self.prior_weight = tf.to_float(tf.equal(self.use_prior, True))
                loss.add(self.prior_loss, weight=self.prior_weight)

            # REINFORCE
            opt_loss = loss.value
            if use_reinforce:

                self.reinforce_imp_weight = self.rec_loss_per_sample
                if not num_steps_prior.analytic:
                    self.reinforce_imp_weight += self.prior_loss.per_sample

                reinforce_loss = self._reinforce(self.reinforce_imp_weight, decay_rate)
                opt_loss += reinforce_loss

            baseline_vars = getattr(self, 'baseline_vars', [])
            model_vars = list(set(tf.trainable_variables()) - set(baseline_vars))
            # L2 reg
            if l2_weight > 0.:
                # don't penalise biases
                weights = [w for w in model_vars if len(w.get_shape()) == 2]
                self.l2_loss = l2_weight * sum(map(tf.nn.l2_loss, weights))
                opt_loss += self.l2_loss
                tf.summary.scalar('l2', self.l2_loss)

            opt = make_opt(self.learning_rate)
            gvs = opt.compute_gradients(opt_loss, var_list=model_vars)

            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                self._train_step = opt.apply_gradients(gvs, global_step=global_step)

            if self.use_reinforce and self.baseline is not None:
                baseline_opt = make_opt(10 * learning_rate)
                self._baseline_tran_step = self._make_baseline_train_step(baseline_opt, self.reinforce_imp_weight,
                                                                          self.baseline, self.baseline_vars)
                self._true_train_step = self._train_step
                self._train_step = tf.group(self._true_train_step, self._baseline_tran_step)

            tf.summary.scalar('num_step', self.num_step)
        # Metrics
        gradient_summaries(gvs)
        self.num_step_accuracy = tf.reduce_mean(tf.to_float(tf.equal(self.gt_num_steps, self.num_step_per_sample)))

        self.loss = loss
        self.opt_loss = opt_loss
        return self._train_step, global_step
