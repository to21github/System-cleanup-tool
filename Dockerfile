FROM ghcr.nju.edu.cn/home-assistant/aarch64-base:latest

# 国内源: Alpine 软件源用中科大, pip 用阿里云
RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.ustc.edu.cn/g' /etc/apk/repositories \
    && apk add --no-cache python3 py3-pip util-linux

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    -r /app/requirements.txt

COPY app /app/app
COPY run.sh /app/run.sh
# host_pid 与 S6 Overlay 不兼容: 覆盖 /init 禁用 S6, 使 host_pid 真正生效
RUN chmod a+x /app/run.sh \
    && rm -f /init \
    && printf '#!/usr/bin/env bash\nexec /app/run.sh\n' > /init \
    && chmod a+x /init

CMD ["/app/run.sh"]
