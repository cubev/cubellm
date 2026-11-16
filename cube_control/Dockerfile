ARG PYTHON_BASE=python:3.13-slim-bookworm
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --no-create-home cubecontrol
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY tests /app/tests
RUN pip install --no-cache-dir --no-deps .
USER 10001:10001
EXPOSE 4001
ENTRYPOINT ["python", "-m", "cube_control"]
CMD ["serve", "--host", "0.0.0.0", "--port", "4001"]
