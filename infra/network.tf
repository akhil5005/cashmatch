# ---------------------------------------------------------------------------
# Network
#
# One public subnet for the application and two private subnets for the
# database. RDS insists on a subnet group spanning at least two availability
# zones even for a single-AZ instance, which is why there are two private
# subnets rather than one.
#
# There is deliberately **no NAT gateway**. A NAT would cost about $32 a
# month -- more than everything else here combined -- and the only thing in
# the private subnets is the database, which has no reason to reach the
# internet. The absence of a route out is itself a security control: even if
# the database host were compromised, it could not phone home.
# ---------------------------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-igw" }
}

# --- public: the application ------------------------------------------------

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.name}-public-a" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --- private: the database --------------------------------------------------

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.1${count.index + 1}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${var.name}-private-${count.index + 1}" }
}

# No routes to the internet gateway. These subnets reach nothing outside the
# VPC, and nothing outside the VPC reaches them.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count = length(aws_subnet.private)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.name}-db-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${var.name}-db-subnets" }
}

# ---------------------------------------------------------------------------
# Security groups
#
# Two groups, and the database one references the application one by id
# rather than by CIDR. That is the point: the rule says "whatever is running
# the application may reach Postgres", not "anything in this address range
# may". Replace the instance and the rule still means what it meant.
# ---------------------------------------------------------------------------

# CloudFront's origin-facing address ranges, published by AWS per region.
data "aws_ec2_managed_prefix_list" "cloudfront_origin" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "CashMatch application host"
  vpc_id      = aws_vpc.main.id

  # Where port 80 may be reached from depends on whether CloudFront is
  # actually in front. With the edge there, every viewer request arrives
  # through it and TLS terminates there, so the instance has no reason to
  # accept a connection from anywhere else -- an origin only CloudFront can
  # reach cannot be port-scanned, fingerprinted, or hit directly over plain
  # HTTP by a viewer who would rather skip the encryption. AWS publishes and
  # maintains the prefix list, which is the point: hard-coding CloudFront's
  # ranges would mean tracking them by hand forever.
  #
  # The list is necessary but not sufficient -- it admits *every* CloudFront
  # distribution, including one belonging to someone else who has learned
  # this address. nginx closes that with a shared secret; see the
  # X-Origin-Verify header in cdn.tf and frontend/default.conf.template.
  dynamic "ingress" {
    for_each = var.enable_cdn ? [1] : []

    content {
      description     = "Plain HTTP, from CloudFront edge locations only"
      from_port       = 80
      to_port         = 80
      protocol        = "tcp"
      prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront_origin.id]
    }
  }

  # And without it, that same narrowing is what takes the site off the
  # internet: the prefix list admits only CloudFront, so with no distribution
  # there is no route in at all. Open is the honest state while that is true.
  dynamic "ingress" {
    for_each = var.enable_cdn ? [] : [1]

    content {
      description = "Plain HTTP, open to the internet -- no CDN in front yet"
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  # Deliberately no port 22. Shell access is via SSM Session Manager, which
  # needs no inbound port, no key pair, and leaves an audit trail in
  # CloudTrail. An open SSH port on a demo box is a liability with no
  # compensating benefit.

  egress {
    description = "Outbound: ECR, SSM, package mirrors"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-app-sg" }
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "CashMatch Postgres"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres, from the application only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  # No egress rule at all. The database has no reason to originate a
  # connection to anything.

  tags = { Name = "${var.name}-db-sg" }
}
