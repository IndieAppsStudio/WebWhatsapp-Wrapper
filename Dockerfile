FROM tiangolo/uwsgi-nginx-flask:python3.8

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --trusted-host pypi.python.org -r requirements.txt
RUN pip install ./