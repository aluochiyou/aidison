FROM python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY engineering_calculate /opt/aidison/entrypoints/engineering_calculate

RUN chmod 0555 /opt/aidison/entrypoints/engineering_calculate

USER 65532:65532

ENTRYPOINT ["/opt/aidison/entrypoints/engineering_calculate"]
