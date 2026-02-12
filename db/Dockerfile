FROM postgres:14-alpine

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN apk add --no-cache postgresql-contrib

COPY ddl.sql /docker-entrypoint-initdb.d/01-ddl.sql

EXPOSE 5432
