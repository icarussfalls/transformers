import torch
import torch.nn as nn
import math
import torch.nn.functional as F
from config import get_config
from utils import *

# input embeddings are created to convert the original sentences into a vector of 512 dimension
# vocab size is the number of unique tokens
# d_model is the size of the embedding vector (dimensionality)
# self.embeddding initializes the embedding layer and maps each token in the vocabulary to  d_model-dimenstion vector

class InputEmbeddings(nn.Module):
    def __init__(self, d_model: int, vocab_size:int ) -> None:
        super().__init__()
        self.d_model = d_model
        self.vocab_size = vocab_size
        self.embedding = nn.Embedding(vocab_size, d_model)
    
    def forward(self, x):
        # (batch_size, seq_len) -> (batch_size, seq_len, d_model):
        # multiply by sqrt(d_model) to scale embeddings according to the paper
        return self.embedding(x) * math.sqrt(self.d_model)
    

class PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, seq_len: int, dropout: float) -> None:
        super().__init__()
        self.d_model = d_model
        self.seq_len = seq_len
        self.dropout = nn.Dropout(dropout)

        # create a matrix of shape (seq_len, d_model)
        pe = torch.zeros(seq_len, d_model)
        # create a vector of shape (seq_len)
        position = torch.arange(0, seq_len, dtype=torch.float).unsqueeze(1) # (seq_len, 1)
        # create a vector of shape (d_model)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * -(math.log(1000.0) / d_model))
        # apply sin to even indices
        pe[:, 0::2] = torch.sin(position * div_term) # sin(position * (1000 ** (2i / d_model)))
        pe[:, 1::2] = torch.cos(position * div_term) # cos(position * (1000 ** (2i / d_model)))
        # add a batch dimension to the position encoding
        pe = pe.unsqueeze(0) # (1, seq_len, d_model)
        # register the position encoding as a buffer
        self.register_buffer('pe', pe) # buffer registered so as not to compute it again

    def forward(self, x):
        x = x + (self.pe[:, :x.shape[1], :]).requires_grad_(False)
        return self.dropout(x)


# d_k is the dimension of the vector processed by the each head == d_model // h
# w_q, w_k, w_v, w_o are linear layers that project the input vectors to queries, keys, values, and outputs resp.

class MultiHeadAttentionBlock(nn.Module):
    def __init__(self, d_model: int, h: int, dropout: float) -> None:
        super().__init__()

        self.d_model = d_model # embedding vector size
        self.h = h # number of heads
        # making sure d_model is divisible by h
        assert d_model % h == 0, "d_model is not divisible by h"

        self.d_k = d_model // h # dimension of the vector seen by each head
        self.w_q = nn.Linear(d_model, d_model, bias=False) # Wq
        self.w_k = nn.Linear(d_model, d_model, bias=False) # Wk
        self.w_v = nn.Linear(d_model, d_model, bias=False) # Wv
        self.w_o = nn.Linear(d_model, d_model, bias=False) # Wo
        self.dropout = nn.Dropout(dropout)

    @staticmethod
    def attention(query, key, value, mask, dropout: nn.Dropout):
        d_k = query.shape[-1]
        # (batch, h, seq_len, d_k) --> (batch, h, seq_len, seq_len)
        attention_scores = (query @ key.transpose(-2, -1)) / math.sqrt(d_k)

        if mask is not None:
            # write a very low value (-inf) to the position where mask == 0
            attention_scores.masked_fill_(mask == 0, torch.finfo(attention_scores.dtype).min)
        attention_scores = attention_scores.softmax(dim=-1) # (batch, h, seq_len, seq_len) apply softmax

        if dropout is not None:
            attention_scores = dropout(attention_scores)
        # (batch, h, seq_len, seq_len) -> (batch, h, seq_len, d_k)
        # return attention scores which can be used for visualization
        return (attention_scores @ value), attention_scores

    def forward(self, q, k, v, mask):
        query = self.w_q(q) # (batch, seq_len, d_model) -> (batch, seq_len, d_model)
        key = self.w_k(k) # (batch, seq_len, d_model) -> (batch, seq_len, d_model)
        value = self.w_v(v) # (batch, seq_len, d_model) -> (batch, seq_len, d_model)

        # need to accomodate h here
        # (batch, seq_len, d_model) -> (batch, seq_len, h, d_k) -> (batch, h, seq_len, dk)
        query = query.view(query.shape[0], query.shape[1], self.h, self.d_k).transpose(1, 2) 
        key = key.view(key.shape[0], key.shape[1], self.h, self.d_k).transpose(1, 2)
        value = value.view(value.shape[0], value.shape[1], self.h, self.d_k).transpose(1, 2)

        # calculate attention
        x, attention_scores = MultiHeadAttentionBlock.attention(query, key, value, mask, self.dropout)

        # combine all the heads together
        # (batch_len, h, seq_len, d_k) -> (batch, seq_len, h, d_k) -> (batch, seq_len, d_model)
        x = x.transpose(1, 2).contiguous().view(x.shape[0], -1, self.h * self.d_k)
        # -1 makes pytorch infer the dimension so becomes seq_len automatically
        # contiguous is required to ensure tensor is stored in the contiguous chunk of memory, needed before .view() after transpose

        # multiply by Wo
        # (batch, seq_len, d_model) -> (batch, seq_len, d_model)
        return self.w_o(x)


class SparseMultiHeadAttentionBlock(nn.Module):
    def __init__(self, d_model:int, h:int, dropout:float, block_size=int, stride=int, causal=False):
        super().__init__()

        self.d_model = d_model
        self.h = h
        self.d_k = d_model // h # this is the dimension of the vector seen by each head

        self.block_size = block_size
        self.stride = stride
        self.causal = causal

        assert d_model % h == 0, "d_model must be divisible by h"

        self.w_q = nn.Linear(d_model, d_model, bias=False)
        self.w_k = nn.Linear(d_model, d_model, bias=False)
        self.w_v = nn.Linear(d_model, d_model, bias=False)
        self.w_o = nn.Linear(d_model, d_model, bias=False)
        self.dropout = nn.Dropout(dropout)

    # def forward(self, x):
    #     B, T, _ = x.shape
    #     q, k , v = self.compute_qkv(x)
    #     attn_mask = self.build_sparse_mask(T, x.device, B)

    #     attn_out = self.compute_attention(q, k, v, attn_mask)
    #     return self.output_projection(attn_out)
    
    def forward(self, q, k, v, mask):
        B, T, _ = q.shape
        q, k, v = self.compute_qkv(q)
        sparse_mask = self.build_sparse_mask(T, q.device, B)  # (B, h, T, T)
        if mask is not None:
            # Expand mask to (B, 1, T, T) if needed, then broadcast to (B, h, T, T)
            if mask.dim() == 3:
                mask = mask.unsqueeze(1)
            mask = mask.expand(B, self.h, T, T)
            attn_mask = sparse_mask & mask
        else:
            attn_mask = sparse_mask
        attn_out = self.compute_attention(q, k, v, attn_mask)
        return self.output_projection(attn_out)


    def compute_qkv(self, x):
        B, T, _ = x.shape
        q = self.w_q(x).view(B, T, self.h, self.d_k).transpose(1,2) # (B, h, T, d_k)
        k = self.w_k(x).view(B, T, self.h, self.d_k).transpose(1, 2)
        v = self.w_v(x).view(B, T, self.h, self.d_k).transpose(1, 2)
        return q, k, v

    def build_sparse_mask(self, seq_len, device, batch_size):
        base_mask = create_sparse_mask(
            seq_len, self.block_size, self.stride, causal=self.causal, device=device) # (1, 1, T, T)
        
        # expand to (B, h, T, T)
        return base_mask.expand(batch_size, self.h, seq_len, seq_len)

    def compute_attention(self, q, k, v, mask):
        # (B, h, T, T)
        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.d_k)
        # Ensure mask is boolean
        if mask.dtype != torch.bool:
            mask = mask.bool()
        scores = scores.masked_fill(~mask, float("-inf"))
        attn_weights = F.softmax(scores, dim=-1)
        attn_weights = self.dropout(attn_weights)
        return torch.matmul(attn_weights, v) # (B, h, T, d_k)

    def output_projection(self, x):
        B, h, T, d_k = x.size()
        x = x.transpose(1, 2).contiguous().view(B, T, h*d_k)
        return self.w_o(x)
    


# alpha is the learnable parameter initialized to one of shape (features,) which scales the normalized outputs
# bias is the learnable parameter initialized to zeros of shape (features,) which shifts the normalized outputs
# eps is to prevent dividing by zero when std is very small
class LayerNormalization(nn.Module):
    def __init__(self, features: int, eps: float = 10**-6) -> None:
        super().__init__()
        self.eps = eps
        self.alpha = nn.Parameter(torch.ones(features))
        self.bias = nn.Parameter(torch.zeros(features))

    def forward(self, x):
        # x: (batch, seq_len, hidden_size)
        # keep the dimension for broadcasting 
        mean = x.mean(dim=-1, keepdim=True)            # shape: (batch, seq_len, 1)
        var = x.var(dim=-1, keepdim=True, unbiased=False)  # shape: (batch, seq_len, 1)
        norm = (x - mean) / torch.sqrt(var + self.eps)     # normalize
        return self.alpha * norm + self.bias
    

class FeedForwardBlock(nn.Module):
    def __init__(self, d_model: int, d_ff: int, dropout: float) -> None:
        super().__init__()
        self.linear1 = nn.Linear(d_model, d_ff) # w1 and b1
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(d_ff, d_model) # w2 and b2

    def forward(self, x):
        # (batch, seq_len, d_model) -> (batch, seq_len, dff) -> (batch, seq_len, d_model)
        return self.linear2(self.dropout(torch.relu(self.linear1(x))))

class ResidualConnection(nn.Module):
    def __init__(self, features: int, dropout: float) -> None:
        super().__init__()
        self.dropout = nn.Dropout(dropout)
        self.norm = LayerNormalization(features)

    def forward(self, x, sublayer):
        return x + self.dropout(sublayer(self.norm(x)))

# this is like the one layer
class EncoderBlock(nn.Module):
    def __init__(self, features: int, self_attention_block : SparseMultiHeadAttentionBlock, feed_forward_block: FeedForwardBlock, dropout: float) -> None:
        super().__init__()
        self.self_attention_block = self_attention_block
        self.feed_forward_block = feed_forward_block
        self.residual_connections = nn.ModuleList([ResidualConnection(features, dropout) for _ in range(2)])

    def forward(self, x, src_mask):
        x = self.residual_connections[0](x, lambda x: self.self_attention_block(x, x, x, src_mask))
        x = self.residual_connections[1](x, self.feed_forward_block)
        return x

# stack of layers, the whole vertical stack in the paper is this class  
class Encoder(nn.Module):
    def __init__(self, features: int, layers: nn.ModuleList) -> None:
        super().__init__()
        self.layers = layers
        self.norm = LayerNormalization(features)

    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)

    
class DecoderBlock(nn.Module):
    def __init__(self, features:int, self_attention_block: SparseMultiHeadAttentionBlock, cross_attention_block: MultiHeadAttentionBlock, feed_forward_block: FeedForwardBlock, dropout: float) -> None:
        super().__init__()
        self.self_attention_block = self_attention_block
        self.cross_attention_block = cross_attention_block
        self.feed_forward_block = feed_forward_block
        self.residual_connection = nn.ModuleList([ResidualConnection(features, dropout) for _ in range(3)])

    def forward(self, x, encoder_output, src_mask, tgt_mask):
        x = self.residual_connection[0](x, lambda x: self.self_attention_block(x, x, x, tgt_mask))
        x = self.residual_connection[1](x, lambda x: self.cross_attention_block(x, encoder_output, encoder_output, src_mask))
        x = self.residual_connection[2](x, self.feed_forward_block)
        # add and norm not applied here at output which is done in decoder class
        return x
    
class Decoder(nn.Module):
    def __init__(self, features: int, layers: nn.ModuleList) -> None:
        super().__init__()
        self.layers = layers
        self.norm = LayerNormalization(features)

    def forward(self, x, encoder_output, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, encoder_output, src_mask, tgt_mask)
        return self.norm(x)

# our code is improved variant on the original google paper, in original paper, normalization is applied after the residual connection, but here, "pre-norm" before the sublayer, then residual added

class ProjectionLayer(nn.Module):
    def __init__(self, d_model, vocab_size):
        super().__init__()
        self.proj = nn.Linear(d_model, vocab_size)

    def forward(self, x):
        # (batch, seq_len, d_model) -> (batch, seq_len, vocab_size)
        return self.proj(x)


class Transformer(nn.Module):
    def __init__(self, encoder: Encoder, decoder: Decoder, src_embed: InputEmbeddings, tgt_embed: InputEmbeddings, src_pos: PositionalEncoding, tgt_pos: PositionalEncoding, projection_layer: ProjectionLayer) -> None:
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.src_embed = src_embed
        self.tgt_embed = tgt_embed
        self.src_pos = src_pos
        self.tgt_pos = tgt_pos
        self.projection_layer = projection_layer

    # def encode(self, src, src_mask):
    #     # (batch, seq_len, d_model)
    #     src = self.src_embed(src)
    #     src = self.src_pos(src)
    #     return self.encoder(src, src_mask)

    # def decode(self, encoder_output: torch.Tensor, src_mask: torch.Tensor, tgt: torch.Tensor, tgt_mask: torch.Tensor):
    #     # (batch, seq_len, d_model)
    #     tgt = self.tgt_embed(tgt)
    #     tgt = self.tgt_pos(tgt)
    #     return self.decoder(tgt, encoder_output, src_mask, tgt_mask)

    # def project(self, x):
    #     # (batch, seq_len, vocab_size)
    #     return self.projection_layer(x)
    
    def forward(self, encoder_input, decoder_input, encoder_mask, decoder_mask):
        # Encode
        src = self.src_embed(encoder_input)
        src = self.src_pos(src)
        encoder_output = self.encoder(src, encoder_mask)
        # Decode
        tgt = self.tgt_embed(decoder_input)
        tgt = self.tgt_pos(tgt)
        decoder_output = self.decoder(tgt, encoder_output, encoder_mask, decoder_mask)
        # Project
        proj_output = self.projection_layer(decoder_output)
        return proj_output
        
    

# lets build the full transformers now

def build_sparse_transformer(src_vocab_size: int, tgt_vocab_size: int, src_seq_len: int, tgt_seq_len: int, d_model: int, N: int, h: int, dropout: float, d_ff: int, block_size:int, stride:int) -> Transformer:    
    # create the embedding layers
    src_embed = InputEmbeddings(d_model, src_vocab_size)
    tgt_embed = InputEmbeddings(d_model, tgt_vocab_size)

    # create the positional encodings
    src_pos = PositionalEncoding(d_model, src_seq_len, dropout)
    tgt_pos = PositionalEncoding(d_model, tgt_seq_len, dropout)

    # create the encoder blocks
    encoder_blocks = []
    for _ in range(N):
        encoder_self_attention_block = SparseMultiHeadAttentionBlock(d_model, h, dropout, block_size=block_size, stride=stride, causal=False)
        feed_forward_block = FeedForwardBlock(d_model, d_ff, dropout)
        encoder_block = EncoderBlock(d_model, encoder_self_attention_block, feed_forward_block, dropout)
        encoder_blocks.append(encoder_block)


    # create the decoder blocks
    decoder_blocks = []
    for _ in range(N):
        decoder_self_attention_block = SparseMultiHeadAttentionBlock(d_model, h, dropout, block_size=block_size, stride=stride, causal=True)
        decoder_cross_attention_block = MultiHeadAttentionBlock(d_model, h, dropout)
        feed_forward_block = FeedForwardBlock(d_model, d_ff, dropout)
        decoder_block = DecoderBlock(d_model, decoder_self_attention_block, decoder_cross_attention_block, feed_forward_block, dropout)
        decoder_blocks.append(decoder_block)

    # create the encoder and decoder
    encoder = Encoder(d_model, nn.ModuleList(encoder_blocks))
    decoder = Decoder(d_model, nn.ModuleList(decoder_blocks))
    
    # create the projection layer
    projection_layer = ProjectionLayer(d_model, tgt_vocab_size)

    # create the transformers
    transformer = Transformer(encoder, decoder, src_embed, tgt_embed, src_pos, tgt_pos, projection_layer)

    # init the params
    for p in transformer.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)
    # xavier uniform (glorot initialization) initializes the weights of neural networks to keep the scale of gradients roughly the same in all layers, help prevent vanishing/exploding gradients
    return transformer



# # === Dummy parameters ===
# batch_size = 2
# src_seq_len = 16
# tgt_seq_len = 16
# src_vocab_size = 100
# tgt_vocab_size = 100
# d_model = 64
# N = 2
# h = 4
# dropout = 0.1
# d_ff = 256

# # === Dummy input tokens (random integers representing word indices) ===
# src_input = torch.randint(0, src_vocab_size, (batch_size, src_seq_len))  # (B, T_src)
# tgt_input = torch.randint(0, tgt_vocab_size, (batch_size, tgt_seq_len))  # (B, T_tgt)

# # === Dummy masks (1 means keep, 0 means pad/mask) ===
# src_mask = torch.ones((batch_size, 1, 1, src_seq_len), dtype=torch.bool)
# tgt_mask = torch.ones((batch_size, 1, tgt_seq_len, tgt_seq_len), dtype=torch.bool)

# # Add causal mask to tgt_mask (prevent future token info)
# causal_mask = torch.tril(torch.ones((tgt_seq_len, tgt_seq_len), dtype=torch.bool))  # (T_tgt, T_tgt)
# tgt_mask = tgt_mask & causal_mask  # (B, 1, T_tgt, T_tgt)


# # === Build the transformer ===
# transformer = build_transformer(
#     src_vocab_size, tgt_vocab_size, src_seq_len, tgt_seq_len,
#     d_model, N, h, dropout, d_ff
# )

# # === Forward pass ===
# output = transformer(src_input, tgt_input, src_mask, tgt_mask)

# print("Output shape:", output.shape)  # Expected: (batch_size, tgt_seq_len, tgt_vocab_size)

