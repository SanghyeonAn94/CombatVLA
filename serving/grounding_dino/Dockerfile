FROM docker.io/nvidia/cuda:13.0.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TORCH_CUDA_ARCH_LIST="10.0"

# 시스템 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 python3.10-dev python3.10-venv python3-pip \
    git ninja-build wget curl ca-certificates \
    libjpeg-dev libpng-dev libgl1 libglib2.0-0 \
    && ln -sf /usr/bin/python3.10 /usr/bin/python \
    && ln -sf /usr/bin/python3.10 /usr/bin/python3 \
    && python -m pip install --upgrade pip setuptools wheel \
    && rm -rf /var/lib/apt/lists/*

# PyTorch 2.10.0 + CUDA 13.0 (B200 sm_100)
RUN pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu130

# GroundingDINO (CUDA ext PyTorch 2.10 호환 패치)
RUN git clone https://github.com/IDEA-Research/GroundingDINO.git /tmp/gdino \
    && cd /tmp/gdino \
    && sed -i 's/^install_torch()/#install_torch()/' setup.py \
    && sed -i 's/\.type()\.is_cuda()/.is_cuda()/g; s/value\.type()/value.scalar_type()/g' groundingdino/models/GroundingDINO/csrc/MsDeformAttn/ms_deform_attn_cuda.cu \
    && pip install -e . --no-build-isolation \
    && rm -rf /tmp/gdino/.git

# transformers 호환 버전 (GroundingDINO BertModel 호환)
RUN pip install transformers==4.44.2

# FastAPI + uvicorn
RUN pip install fastapi uvicorn python-multipart pillow

WORKDIR /app
COPY server.py /app/server.py

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
