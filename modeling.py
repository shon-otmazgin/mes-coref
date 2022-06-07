import numpy as np
import torch
from torch import nn
from torch.nn import Module, Linear, LayerNorm, Dropout
from transformers import BertPreTrainedModel, LongformerModel, RobertaModel, AutoModel
from transformers.activations import ACT2FN

from consts import CATEGORIES, STOPWORDS
from util import extract_clusters, extract_mentions_to_predicted_clusters_from_clusters, mask_tensor, \
    is_pronoun, get_head_id, get_head_id2


class FullyConnectedLayer(Module):
    def __init__(self, config, input_dim, output_dim, dropout_prob):
        super(FullyConnectedLayer, self).__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.dropout_prob = dropout_prob

        self.dense = Linear(self.input_dim, self.output_dim)
        self.layer_norm = LayerNorm(self.output_dim, eps=config.layer_norm_eps)
        self.activation_func = ACT2FN[config.hidden_act]
        self.dropout = Dropout(self.dropout_prob)

    def forward(self, inputs):
        temp = inputs
        temp = self.dense(temp)
        temp = self.activation_func(temp)
        temp = self.layer_norm(temp)
        temp = self.dropout(temp)
        return temp


class LingMessCoref(BertPreTrainedModel):
    def __init__(self, config, args):
        super().__init__(config)
        self.max_span_length = args.max_span_length
        self.top_lambda = args.top_lambda
        self.ffnn_size = args.ffnn_size
        self.num_cats = len(CATEGORIES) + 1                # for sharing
        self.all_head_size = self.ffnn_size * self.num_cats

        self.longformer = LongformerModel(config)

        self.start_mention_mlp = FullyConnectedLayer(config, config.hidden_size, self.ffnn_size, args.dropout_prob)
        self.end_mention_mlp = FullyConnectedLayer(config, config.hidden_size, self.ffnn_size, args.dropout_prob)

        self.mention_start_classifier = Linear(self.ffnn_size, 1)
        self.mention_end_classifier = Linear(self.ffnn_size, 1)
        self.mention_s2e_classifier = Linear(self.ffnn_size, self.ffnn_size)

        self.antecedent_s2s_classifier_cat = nn.ModuleList([Linear(self.ffnn_size, self.ffnn_size) for _ in range(self.num_cats)])
        self.antecedent_e2e_classifier_cat = nn.ModuleList([Linear(self.ffnn_size, self.ffnn_size) for _ in range(self.num_cats)])
        self.antecedent_s2e_classifier_cat = nn.ModuleList([Linear(self.ffnn_size, self.ffnn_size) for _ in range(self.num_cats)])
        self.antecedent_e2s_classifier_cat = nn.ModuleList([Linear(self.ffnn_size, self.ffnn_size) for _ in range(self.num_cats)])

        self.start_coref_mlp_cat = nn.ModuleList([FullyConnectedLayer(config, config.hidden_size, self.ffnn_size, args.dropout_prob)
                                                  for _ in range(self.num_cats)])

        self.end_coref_mlp_cat = nn.ModuleList([FullyConnectedLayer(config, config.hidden_size, self.ffnn_size, args.dropout_prob)
                                                for _ in range(self.num_cats)])

        self.init_weights()

    def num_parameters(self) -> tuple:
        def head_filter(x):
            return x[1].requires_grad and any(hp in x[0] for hp in ['coref', 'mention', 'antecedent'])

        head_params = filter(head_filter, self.named_parameters())
        head_params = sum(p.numel() for n, p in head_params)
        return super().num_parameters() - head_params, head_params

    def _get_span_mask(self, batch_size, k, max_k):
        """
        :param batch_size: int
        :param k: tensor of size [batch_size], with the required k for each example
        :param max_k: int
        :return: [batch_size, max_k] of zero-ones, where 1 stands for a valid span and 0 for a padded span
        """
        size = (batch_size, max_k)
        idx = torch.arange(max_k, device=self.device).unsqueeze(0).expand(size)
        len_expanded = k.unsqueeze(1).expand(size)
        return (idx < len_expanded).int()

    def _prune_topk_mentions(self, mention_logits, attention_mask):
        """
        :param mention_logits: Shape [batch_size, seq_length, seq_length]
        :param attention_mask: [batch_size, seq_length]
        :param top_lambda:
        :return:
        """
        batch_size, seq_length, _ = mention_logits.size()
        actual_seq_lengths = torch.sum(attention_mask, dim=-1)  # [batch_size]

        k = (actual_seq_lengths * self.top_lambda).int()  # [batch_size]
        max_k = int(torch.max(k))  # This is the k for the largest input in the batch, we will need to pad

        _, topk_1d_indices = torch.topk(mention_logits.view(batch_size, -1), dim=-1, k=max_k)  # [batch_size, max_k]

        span_mask = self._get_span_mask(batch_size, k, max_k)  # [batch_size, max_k]
        # drop the invalid indices and set them to the last index
        topk_1d_indices = (topk_1d_indices * span_mask) + (1 - span_mask) * ((seq_length ** 2) - 1)  # We take different k for each example
        # sorting for coref mention order
        sorted_topk_1d_indices, _ = torch.sort(topk_1d_indices, dim=-1)  # [batch_size, max_k]

        # gives the row index in 2D matrix
        topk_mention_start_ids = torch.div(sorted_topk_1d_indices, seq_length, rounding_mode='floor') # [batch_size, max_k]
        topk_mention_end_ids = sorted_topk_1d_indices % seq_length  # [batch_size, max_k]

        topk_mention_logits = mention_logits[torch.arange(batch_size).unsqueeze(-1).expand(batch_size, max_k),
                                             topk_mention_start_ids, topk_mention_end_ids]  # [batch_size, max_k]

        # this is antecedents scores - rows mentions, cols coref mentions
        topk_mention_logits = topk_mention_logits.unsqueeze(-1) + topk_mention_logits.unsqueeze(-2)  # [batch_size, max_k, max_k]

        return topk_mention_start_ids, topk_mention_end_ids, span_mask, topk_mention_logits

    def _mask_antecedent_logits(self, antecedent_logits, span_mask, categories_masks=None):
        antecedents_mask = torch.ones_like(antecedent_logits, dtype=self.dtype).tril(diagonal=-1)

        if categories_masks is not None:
            mask = antecedents_mask * span_mask.unsqueeze(1).unsqueeze(-1)
            mask *= categories_masks
        else:
            mask = antecedents_mask * span_mask.unsqueeze(-1)

        antecedent_logits = mask_tensor(antecedent_logits, mask)
        return antecedent_logits

    def _get_clusters_labels(self, span_starts, span_ends, all_clusters):
        """
        :param span_starts: [batch_size, max_k]
        :param span_ends: [batch_size, max_k]
        :param all_clusters: [batch_size, max_cluster_size, max_clusters_num, 2]
        :return: [batch_size, max_k, max_k + 1] - [b, i, j] == 1 if j is antecedent of i
        """
        batch_size, max_k = span_starts.size()
        new_cluster_labels = np.zeros((batch_size, max_k, max_k))

        span_starts_cpu = span_starts.cpu().tolist()
        span_ends_cpu = span_ends.cpu().tolist()
        all_clusters_cpu = all_clusters.cpu().tolist()

        for b, (starts, ends, gold_clusters) in enumerate(zip(span_starts_cpu, span_ends_cpu, all_clusters_cpu)):
            gold_clusters = extract_clusters(gold_clusters)
            mention_to_gold_clusters = extract_mentions_to_predicted_clusters_from_clusters(gold_clusters)
            for i, (start, end) in enumerate(zip(starts, ends)):
                if (start, end) not in mention_to_gold_clusters:
                    continue
                for j, (a_start, a_end) in enumerate(list(zip(starts, ends))[:i]):
                    if (a_start, a_end) in mention_to_gold_clusters[(start, end)]:
                        new_cluster_labels[b, i, j] = 1

        new_cluster_labels = torch.tensor(new_cluster_labels, device=self.device)
        return new_cluster_labels

    def _get_pairs_categories(self, texts, subtoken_map, span_starts, span_ends):
        batch_size, max_k = span_starts.size()

        spans = []
        for b, (starts, ends) in enumerate(zip(span_starts.cpu().tolist(), span_ends.cpu().tolist())):
            doc_spans = []
            for start, end in zip(starts, ends):
                word_indices = sorted(set(subtoken_map[b][start:end + 1]) - {None})
                span = {texts[b][idx].lower() for idx in word_indices}
                pronoun_id = is_pronoun(span)
                doc_spans.append((span - STOPWORDS, pronoun_id))
            spans.append(doc_spans)

        categories_labels = np.zeros((batch_size, max_k, max_k)) - 1
        for b in range(batch_size):
            for i in range(max_k):
                for j in list(range(max_k))[:i]:
                    categories_labels[b, i, j] = get_head_id2(spans[b][i], spans[b][j])

        categories_labels = torch.tensor(categories_labels, device=self.device)
        masks = [categories_labels == cat_id for cat_id in range(self.num_cats - 1)] + [categories_labels != -1]
        masks = torch.stack(masks, dim=1).int()
        return categories_labels, masks

    def _get_marginal_log_likelihood_loss(self, logits, labels, span_mask):
        gold_coref_logits = mask_tensor(logits, labels)                       # [batch_size, num_cats + 1, max_k, max_k]

        gold_log_sum_exp = torch.logsumexp(gold_coref_logits, dim=-1)         # [batch_size, num_cats + 1, max_k]
        all_log_sum_exp = torch.logsumexp(logits, dim=-1)                     # [batch_size, num_cats + 1, max_k]
        losses = all_log_sum_exp - gold_log_sum_exp                           # [batch_size, num_cats + 1, max_k]

        # zero the loss of padded spans
        span_mask = span_mask.unsqueeze(1)                                    # [batch_size, 1, max_k]
        losses = losses * span_mask                                           # [batch_size, num_cats, max_k]

        # normalize loss by spans
        per_span_loss = losses.mean(dim=-1)                                   # [batch_size, num_cats + 1]

        # normalize loss by document
        loss_per_cat = per_span_loss.mean(dim=0)                              # [num_cats + 1]

        # normalize loss by category
        loss = loss_per_cat.sum()
        return loss

    def _get_mention_mask(self, mention_logits_or_weights):
        """
        Returns a tensor of size [batch_size, seq_length, seq_length] where valid spans
        (start <= end < start + max_span_length) are 1 and the rest are 0
        :param mention_logits_or_weights: Either the span mention logits or weights, size [batch_size, seq_length, seq_length]
        """
        mention_mask = torch.ones_like(mention_logits_or_weights, dtype=self.dtype)
        mention_mask = mention_mask.triu(diagonal=0)
        mention_mask = mention_mask.tril(diagonal=self.max_span_length - 1)
        return mention_mask

    def _calc_mention_logits(self, start_mention_reps, end_mention_reps):
        start_mention_logits = self.mention_start_classifier(start_mention_reps).squeeze(-1)  # [batch_size, seq_length]
        end_mention_logits = self.mention_end_classifier(end_mention_reps).squeeze(-1)  # [batch_size, seq_length]

        temp = self.mention_s2e_classifier(start_mention_reps)  # [batch_size, seq_length]
        joint_mention_logits = torch.matmul(temp,
                                            end_mention_reps.permute([0, 2, 1]))  # [batch_size, seq_length, seq_length]

        mention_logits = joint_mention_logits + start_mention_logits.unsqueeze(-1) + end_mention_logits.unsqueeze(-2)
        mention_mask = self._get_mention_mask(mention_logits)  # [batch_size, seq_length, seq_length]
        mention_logits = mask_tensor(mention_logits, mention_mask)  # [batch_size, seq_length, seq_length]
        return mention_logits

    def _calc_coref_head_logits(self, top_k_start_coref_reps, top_k_end_coref_reps, cat_id):
        # s2s
        temp = self.antecedent_s2s_classifier_cat[cat_id](top_k_start_coref_reps)  # [batch_size, max_k, dim]
        top_k_s2s_coref_logits = torch.matmul(temp,
                                              top_k_start_coref_reps.permute([0, 2, 1]))  # [batch_size, max_k, max_k]

        # e2e
        temp = self.antecedent_e2e_classifier_cat[cat_id](top_k_end_coref_reps)  # [batch_size, max_k, dim]
        top_k_e2e_coref_logits = torch.matmul(temp,
                                              top_k_end_coref_reps.permute([0, 2, 1]))  # [batch_size, max_k, max_k]

        # s2e
        temp = self.antecedent_s2e_classifier_cat[cat_id](top_k_start_coref_reps)  # [batch_size, max_k, dim]
        top_k_s2e_coref_logits = torch.matmul(temp,
                                              top_k_end_coref_reps.permute([0, 2, 1]))  # [batch_size, max_k, max_k]

        # e2s
        temp = self.antecedent_e2s_classifier_cat[cat_id](top_k_end_coref_reps)  # [batch_size, max_k, dim]
        top_k_e2s_coref_logits = torch.matmul(temp,
                                              top_k_start_coref_reps.permute([0, 2, 1]))  # [batch_size, max_k, max_k]

        # sum all terms
        coref_logits = top_k_s2e_coref_logits + top_k_e2s_coref_logits + top_k_s2s_coref_logits + top_k_e2e_coref_logits  # [batch_size, max_k, max_k]
        return coref_logits

    def _calc_coref_logits(self, sequence_output, mention_start_ids, mention_end_ids):
        batch_size, max_k = mention_start_ids.size()
        size = (batch_size, max_k, self.ffnn_size)

        cat_logit_list = []
        for cat_id in range(self.num_cats):
            start_coref_reps = self.start_coref_mlp_cat[cat_id](sequence_output)
            end_coref_reps = self.end_coref_mlp_cat[cat_id](sequence_output)

            # gather reps
            topk_start_coref_reps = torch.gather(start_coref_reps, dim=1, index=mention_start_ids.unsqueeze(-1).expand(size))
            topk_end_coref_reps = torch.gather(end_coref_reps, dim=1, index=mention_end_ids.unsqueeze(-1).expand(size))

            cat_coref_logits = self._calc_coref_head_logits(topk_start_coref_reps, topk_end_coref_reps, cat_id)

            cat_logit_list.append(cat_coref_logits)

        return torch.stack(cat_logit_list, dim=1)

    def _get_all_labels(self, clusters_labels, categories_masks):
        batch_size, max_k, _ = clusters_labels.size()

        categories_labels = clusters_labels.unsqueeze(1).repeat(1, self.num_cats, 1, 1) * categories_masks
        all_labels = torch.cat((categories_labels, clusters_labels.unsqueeze(1)), dim=1)      # for the combined loss (L_coref)

        # null cluster
        zeros = torch.zeros((batch_size, self.num_cats + 1, max_k, 1), device=self.device)
        all_labels = torch.cat((all_labels, zeros), dim=-1)                      # [batch_size, num_cats + 1, max_k, max_k + 1]
        no_antecedents = 1 - torch.sum(all_labels, dim=-1).bool().float()
        all_labels[:, :, :, -1] = no_antecedents

        return all_labels

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_cats, self.ffnn_size)
        x = x.view(new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, batch, gold_clusters=None, return_all_outputs=False):
        subtoken_map = batch['subtoken_map']
        texts = batch['text']
        input_ids = batch['input_ids']
        attention_mask = batch['attention_mask']

        outputs = self.longformer(input_ids, attention_mask=attention_mask)
        sequence_output = outputs.last_hidden_state

        # Compute representations
        start_mention_reps = self.start_mention_mlp(sequence_output)
        end_mention_reps = self.end_mention_mlp(sequence_output)

        # mention scores
        mention_logits = self._calc_mention_logits(start_mention_reps, end_mention_reps)

        # prune mentions
        mention_start_ids, mention_end_ids, span_mask, topk_mention_logits = self._prune_topk_mentions(mention_logits, attention_mask)

        batch_size, max_k = mention_start_ids.size()

        categories_labels, categories_masks = self._get_pairs_categories(
            texts, subtoken_map, mention_start_ids, mention_end_ids
        )
        categories_logits = self._calc_coref_logits(sequence_output, mention_start_ids, mention_end_ids)

        final_logits = (categories_logits * categories_masks).sum(dim=1) + topk_mention_logits
        categories_logits = categories_logits + topk_mention_logits.unsqueeze(1)

        final_logits = self._mask_antecedent_logits(final_logits, span_mask)
        categories_logits = self._mask_antecedent_logits(categories_logits, span_mask, categories_masks)

        # adding zero logits for null span
        final_logits = torch.cat((final_logits, torch.zeros((batch_size, max_k, 1), device=self.device)), dim=-1)  # [batch_size, max_k, max_k + 1]
        categories_logits = torch.cat((categories_logits, torch.zeros((batch_size, self.num_cats, max_k, 1), device=self.device)), dim=-1)  # [batch_size, max_k, max_k + 1]

        if return_all_outputs:
            outputs = (mention_start_ids, mention_end_ids, final_logits, categories_labels)
        else:
            outputs = tuple()

        if gold_clusters is not None:
            clusters_labels = self._get_clusters_labels(mention_start_ids, mention_end_ids, gold_clusters)
            all_labels = self._get_all_labels(clusters_labels, categories_masks)
            all_logits = torch.cat((categories_logits, final_logits.unsqueeze(1)), dim=1)

            loss = self._get_marginal_log_likelihood_loss(all_logits, all_labels, span_mask)
            outputs = (loss,) + outputs

        return outputs





